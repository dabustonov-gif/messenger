package com.securemsg.app.crypto

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.security.keystore.StrongBoxUnavailableException
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Stores the account secrets (identity keys, DB passphrase, session token) encrypted with an
 * AES-256-GCM key that never leaves the Android Keystore (StrongBox / Titan M2 on Pixel when available).
 */
class Vault(context: Context) {
    private val prefs = context.getSharedPreferences("vault", Context.MODE_PRIVATE)
    private val alias = "msgr_master_v1"

    data class Account(
        val server: String,
        val username: String,
        val signSeed: ByteArray,
        val boxSk: ByteArray,
        val dbPass: ByteArray,
        val token: String?,
    ) {
        val keys by lazy { E2E.Keys(signSeed, boxSk) }
    }

    private fun masterKey(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(alias, null) as? SecretKey)?.let { return it }
        fun gen(strongBox: Boolean): SecretKey {
            val spec = KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .setIsStrongBoxBacked(strongBox)
                .build()
            return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run { init(spec); generateKey() }
        }
        return try { gen(true) } catch (_: StrongBoxUnavailableException) { gen(false) }
    }

    private fun encrypt(plain: ByteArray): String {
        val c = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, masterKey()) }
        return E2E.b64(c.iv + c.doFinal(plain))
    }

    private fun decrypt(s: String): ByteArray {
        val raw = E2E.unb64(s)
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.DECRYPT_MODE, masterKey(), GCMParameterSpec(128, raw, 0, 12))
        return c.doFinal(raw, 12, raw.size - 12)
    }

    fun load(): Account? {
        val blob = prefs.getString("account", null) ?: return null
        val j = JSONObject(String(decrypt(blob)))
        return Account(
            j.getString("server"), j.getString("username"),
            E2E.unb64(j.getString("sign_seed")), E2E.unb64(j.getString("box_sk")),
            E2E.unb64(j.getString("db_pass")), j.optString("token").ifEmpty { null },
        )
    }

    fun save(a: Account) {
        val j = JSONObject()
            .put("server", a.server).put("username", a.username)
            .put("sign_seed", E2E.b64(a.signSeed)).put("box_sk", E2E.b64(a.boxSk))
            .put("db_pass", E2E.b64(a.dbPass)).put("token", a.token ?: "")
        prefs.edit().putString("account", encrypt(j.toString().toByteArray())).apply()
    }

    fun wipe() {
        prefs.edit().clear().apply()
        KeyStore.getInstance("AndroidKeyStore").apply { load(null) }.deleteEntry(alias)
    }
}
