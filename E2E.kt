package com.securemsg.app.crypto

import android.util.Base64
import com.goterl.lazysodium.SodiumAndroid
import java.security.MessageDigest
import javax.crypto.AEADBadTagException
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Сквозное шифрование, протокол v2 (побайтно совпадает с server/tests/refclient.py).
 *
 * Личность:  Ed25519 (подпись) + X25519 (обмен ключами); ключ X25519 подписан ключом Ed25519.
 * Сообщение: body = nonce(12) || AES-256-GCM(msgKey, json, ad="msgr-body-v2")
 *            для каждого получателя:
 *              wk  = SHA-256("msgr-wrap-v2" || X25519(я, он) || salt || pkОтправителя || pkПолучателя)
 *              key = salt(16) || nonce(12) || AES-256-GCM(wk, msgKey, ad = SHA-256(body))
 * Файл:      blob = nonce(12) || AES-256-GCM(fileKey, data, ad="msgr-file-v2"); fileKey передаётся внутри body.
 *
 * AES-GCM выполняется аппаратно (ARMv8 Crypto Extensions) через стандартный javax.crypto,
 * libsodium используется только для кривых (X25519, Ed25519) и CSPRNG.
 */
object E2E {
    private val na: SodiumAndroid = SodiumAndroid().also { check(it.sodium_init() >= 0) }

    private val BODY_AD = "msgr-body-v2".toByteArray()
    private val WRAP_CTX = "msgr-wrap-v2".toByteArray()
    private val FILE_AD = "msgr-file-v2".toByteArray()
    val BOXKEY_CTX = "msgr-boxkey-v1".toByteArray()
    val REGISTER_CTX = "msgr-register-v1".toByteArray()
    val AUTH_CTX = "msgr-auth-v1".toByteArray()

    fun random(n: Int) = ByteArray(n).also { na.randombytes_buf(it, n) }

    fun sha256(vararg parts: ByteArray): ByteArray =
        MessageDigest.getInstance("SHA-256").run { parts.forEach { update(it) }; digest() }

    fun b64(b: ByteArray): String = Base64.encodeToString(b, Base64.NO_WRAP)
    fun unb64(s: String): ByteArray = Base64.decode(s, Base64.NO_WRAP)

    // ---------------------------------------------------------------- primitives
    /** AES-256-GCM: возвращает ciphertext || tag(16). Nonce всегда случайный и одноразовый. */
    fun seal(key: ByteArray, nonce: ByteArray, msg: ByteArray, ad: ByteArray): ByteArray {
        require(key.size == 32 && nonce.size == 12)
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
        c.updateAAD(ad)
        return c.doFinal(msg)
    }

    /** Расшифровка с проверкой тега; null — если данные подменены или ключ не тот. */
    fun open(key: ByteArray, nonce: ByteArray, c: ByteArray, ad: ByteArray): ByteArray? {
        if (c.size < 16 || key.size != 32 || nonce.size != 12) return null
        return try {
            val d = Cipher.getInstance("AES/GCM/NoPadding")
            d.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
            d.updateAAD(ad)
            d.doFinal(c)
        } catch (_: AEADBadTagException) { null }
    }

    fun x25519(sk: ByteArray, pk: ByteArray): ByteArray =
        ByteArray(32).also { check(na.crypto_scalarmult(it, sk, pk) == 0) { "bad peer key" } }

    // ---------------------------------------------------------------- identity
    class Keys(val signSeed: ByteArray, val boxSk: ByteArray) {
        val signPk = ByteArray(32)
        val signSk = ByteArray(64)
        val boxPk = ByteArray(32)
        init {
            check(na.crypto_sign_seed_keypair(signPk, signSk, signSeed) == 0)
            check(na.crypto_scalarmult_base(boxPk, boxSk) == 0)
        }
        fun sign(msg: ByteArray): ByteArray =
            ByteArray(64).also { check(na.crypto_sign_detached(it, null, msg, msg.size.toLong(), signSk) == 0) }
    }

    fun newKeys() = Keys(random(32), random(32))

    fun verify(pk: ByteArray, sig: ByteArray, msg: ByteArray): Boolean =
        sig.size == 64 && pk.size == 32 && na.crypto_sign_verify_detached(sig, msg, msg.size.toLong(), pk) == 0

    /** Returns the peer's X25519 key only if it is signed by the peer's Ed25519 key. */
    fun verifiedBoxKey(signPk: ByteArray, boxPk: ByteArray, boxSig: ByteArray): ByteArray? =
        if (boxPk.size == 32 && verify(signPk, boxSig, BOXKEY_CTX + boxPk)) boxPk else null

    // ---------------------------------------------------------------- messages
    class Envelope(val body: String, val keys: Map<String, String>)

    fun encrypt(me: Keys, recipients: Map<String, ByteArray>, plaintext: ByteArray): Envelope {
        val msgKey = random(32)
        val nonce = random(12)
        val body = nonce + seal(msgKey, nonce, plaintext, BODY_AD)
        val bodyHash = sha256(body)
        val keys = recipients.mapValues { (_, pk) ->
            val salt = random(16)
            val n2 = random(12)
            val wk = sha256(WRAP_CTX, x25519(me.boxSk, pk), salt, me.boxPk, pk)
            b64(salt + n2 + seal(wk, n2, msgKey, bodyHash))
        }
        msgKey.fill(0)
        return Envelope(b64(body), keys)
    }

    fun decrypt(me: Keys, senderBoxPk: ByteArray, bodyB64: String, keyB64: String): ByteArray? = runCatching {
        val body = unb64(bodyB64)
        val w = unb64(keyB64)
        if (w.size < 16 + 12 + 16 || body.size < 12 + 16) return null
        val wk = sha256(WRAP_CTX, x25519(me.boxSk, senderBoxPk), w.copyOfRange(0, 16), senderBoxPk, me.boxPk)
        val msgKey = open(wk, w.copyOfRange(16, 28), w.copyOfRange(28, w.size), sha256(body)) ?: return null
        open(msgKey, body.copyOfRange(0, 12), body.copyOfRange(12, body.size), BODY_AD)
    }.getOrNull()

    // ---------------------------------------------------------------- files
    fun encryptFile(data: ByteArray): Pair<ByteArray, ByteArray> {
        val key = random(32)
        val nonce = random(12)
        return (nonce + seal(key, nonce, data, FILE_AD)) to key
    }

    fun decryptFile(blob: ByteArray, key: ByteArray): ByteArray? =
        if (blob.size < 28) null else open(key, blob.copyOfRange(0, 12), blob.copyOfRange(12, blob.size), FILE_AD)

    // ---------------------------------------------------------------- verification
    /** 60-digit safety number for comparing in person; identical on both phones. */
    fun safetyNumber(a: ByteArray, b: ByteArray): String {
        val (x, y) = if (b64(a) < b64(b)) a to b else b to a
        val d = MessageDigest.getInstance("SHA-512").digest("msgr-safety-v1".toByteArray() + x + y)
        return (0 until 12).joinToString(" ") { i ->
            var v = 0L
            for (j in 0 until 5) v = (v shl 8) or (d[i * 5 + j].toLong() and 0xff)
            "%05d".format(v % 100000)
        }
    }
}
