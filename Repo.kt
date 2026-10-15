package com.securemsg.app

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import com.securemsg.app.crypto.E2E
import com.securemsg.app.crypto.Vault
import com.securemsg.app.data.AppDb
import com.securemsg.app.data.Contact
import com.securemsg.app.data.Conversation
import com.securemsg.app.data.Message
import com.securemsg.app.net.Api
import com.securemsg.app.net.ApiException
import com.securemsg.app.service.Notifier
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID

class KeyChangedException(val username: String) : Exception("keys changed: $username")

/** Single place for account, crypto, relay connection and local storage. */
object Repo {
    const val MAX_FILE = 50 * 1024 * 1024

    private lateinit var app: Context
    private lateinit var vault: Vault
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    val account = MutableStateFlow<Vault.Account?>(null)
    val online = MutableStateFlow(false)
    val events = MutableSharedFlow<String>(extraBufferCapacity = 16)   // short user-facing notices
    @Volatile var activeConversation: String? = null

    private var db: AppDb? = null
    val dao get() = db!!.dao()
    private var api: Api? = null
    private var ws: WebSocket? = null
    private var wantConnected = false
    private var backoffMs = 1000L
    private val incomingLock = Mutex()

    val me: String get() = account.value!!.username

    fun init(context: Context) {
        app = context.applicationContext
        vault = Vault(app)
        runCatching { vault.load() }.getOrNull()?.let { openSession(it) }
    }

    private fun openSession(a: Vault.Account) {
        db = AppDb.open(app, a.dbPass.copyOf())
        api = Api(app, a.server).also { it.token = a.token }
        account.value = a
    }

    // ================================================================ account
    suspend fun register(server: String, username: String, invite: String) {
        val name = username.trim().lowercase()
        val keys = E2E.newKeys()
        val tmp = Api(app, server.trim())
        tmp.post("/api/register", JSONObject()
            .put("username", name).put("invite", invite.trim())
            .put("sign_pk", E2E.b64(keys.signPk)).put("box_pk", E2E.b64(keys.boxPk))
            .put("box_sig", E2E.b64(keys.sign(E2E.BOXKEY_CTX + keys.boxPk)))
            .put("reg_sig", E2E.b64(keys.sign(E2E.REGISTER_CTX + name.toByteArray()))))
        var acc = Vault.Account(tmp.base, name, keys.signSeed, keys.boxSk, E2E.random(32), null)
        acc = acc.copy(token = login(tmp, acc))
        vault.save(acc)
        openSession(acc)
    }

    private suspend fun login(a: Api, acc: Vault.Account): String {
        val nonce = E2E.unb64(a.post("/api/auth/challenge", JSONObject().put("username", acc.username)).getString("nonce"))
        val sig = acc.keys.sign(E2E.AUTH_CTX + nonce)
        return a.post("/api/auth/verify", JSONObject().put("username", acc.username).put("signature", E2E.b64(sig)))
            .getString("token")
    }

    private suspend fun relogin() {
        val acc = account.value ?: return
        val t = login(api!!, acc)
        api!!.token = t
        val updated = acc.copy(token = t)
        vault.save(updated)
        account.value = updated
    }

    /** Runs an authenticated call, logging in again once if the session expired. */
    private suspend fun <T> authed(block: suspend (Api) -> T): T = try {
        block(api!!)
    } catch (e: ApiException) {
        if (e.http != 401) throw e
        relogin(); block(api!!)
    }

    fun wipeEverything() {
        disconnect()
        db?.close(); db = null
        app.deleteDatabase("chats.db")
        File(app.cacheDir, "att").deleteRecursively()
        vault.wipe()
        account.value = null
    }

    suspend fun createInvite(): String = authed { it.post("/api/invites").getString("invite") }

    // ================================================================ relay connection
    fun connect() {
        if (account.value == null) return
        wantConnected = true
        if (ws == null) openSocket()
    }

    fun disconnect() {
        wantConnected = false
        ws?.close(1000, null); ws = null
        online.value = false
    }

    private fun openSocket() {
        val a = api ?: return
        ws = a.openSocket(object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                webSocket.send(JSONObject().put("op", "auth").put("token", a.token ?: "").toString())
            }
            override fun onMessage(webSocket: WebSocket, text: String) {
                scope.launch { runCatching { onFrame(JSONObject(text)) } }
            }
            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
                if (code == 4401) scope.launch { runCatching { relogin() } }
                dropped(webSocket)
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = dropped(webSocket)
        })
    }

    private fun dropped(socket: WebSocket) {
        if (ws !== socket) return
        ws = null
        online.value = false
        if (!wantConnected) return
        val wait = backoffMs
        backoffMs = (backoffMs * 2).coerceAtMost(60_000)
        scope.launch { delay(wait); if (wantConnected && ws == null) openSocket() }
    }

    private suspend fun onFrame(j: JSONObject) {
        when (j.optString("op")) {
            "ready" -> {
                online.value = true
                backoffMs = 1000
                runCatching { refreshGroups() }
                dao.unsent().forEach { transmit(it) }
            }
            "msg" -> incomingLock.withLock { onIncoming(j) }
            "sent" -> dao.setStatus(j.getString("cid"), Message.STATUS_SENT, j.getLong("ts") * 1000)
            "group" -> applyGroup(j.optJSONObject("group"), j.getLong("group_id"))
            "error" -> {
                val cid = j.optString("cid").ifEmpty { null }
                if (j.optString("error") == "members_changed" && cid != null) {
                    applyGroup(j.optJSONObject("group"), j.getJSONObject("group").getLong("id"))
                    dao.byCid(cid)?.let { transmit(it) }          // re-encrypt for the current members
                } else if (cid != null) {
                    dao.setStatus(cid, Message.STATUS_FAILED, 0)
                    events.tryEmit("Не отправлено: ${j.optString("error")}")
                }
            }
        }
    }

    // ================================================================ contacts & keys (TOFU)
    private suspend fun fetchUser(username: String): Contact? {
        val u = try { authed { it.get("/api/users/$username") } } catch (e: ApiException) {
            if (e.http == 404) return null else throw e
        }
        val signPk = E2E.unb64(u.getString("sign_pk"))
        val boxPk = E2E.verifiedBoxKey(signPk, E2E.unb64(u.getString("box_pk")), E2E.unb64(u.getString("box_sig")))
            ?: throw SecurityException("Сервер вернул неподписанный ключ для $username")
        return Contact(username, E2E.b64(signPk), E2E.b64(boxPk))
    }

    /** Fetches the peer's keys; first time they are pinned, a later change is held as "pending". */
    suspend fun refreshContact(username: String): Contact? {
        val fresh = fetchUser(username) ?: return null
        val known = dao.contact(username)
        val result = when {
            known == null -> fresh
            known.signPk == fresh.signPk && known.boxPk == fresh.boxPk -> known.copy(pendingSignPk = null, pendingBoxPk = null)
            else -> known.copy(pendingSignPk = fresh.signPk, pendingBoxPk = fresh.boxPk)
        }
        dao.putContact(result)
        if (result.pendingSignPk != null) events.tryEmit("⚠ У $username изменились ключи — проверьте код безопасности")
        return result
    }

    private suspend fun boxKeyOf(username: String): ByteArray {
        val c = dao.contact(username) ?: refreshContact(username) ?: throw IllegalStateException("Нет пользователя $username")
        if (c.pendingSignPk != null) throw KeyChangedException(username)
        return E2E.unb64(c.boxPk)
    }

    suspend fun acceptNewKeys(username: String) {
        val c = dao.contact(username) ?: return
        if (c.pendingSignPk == null) return
        dao.putContact(Contact(username, c.pendingSignPk, c.pendingBoxPk!!, verified = false))
        disconnect(); connect()   // reconnect so the server redelivers messages held for this contact
    }

    suspend fun setVerified(username: String, v: Boolean) {
        dao.contact(username)?.let { dao.putContact(it.copy(verified = v)) }
    }

    fun safetyNumber(c: Contact): String = E2E.safetyNumber(account.value!!.keys.signPk, E2E.unb64(c.signPk))

    // ================================================================ conversations
    suspend fun startDirect(username: String): String {
        val name = username.trim().lowercase()
        require(name != me) { "Это вы" }
        refreshContact(name) ?: throw IllegalArgumentException("Пользователь не найден")
        val id = "u:$name"
        if (dao.conversation(id) == null) dao.upsertConversation(Conversation(id, name))
        return id
    }

    suspend fun createGroup(name: String, members: List<String>): String {
        val arr = JSONArray(members.map { it.trim().lowercase() }.filter { it.isNotEmpty() })
        val g = authed { it.post("/api/groups", JSONObject().put("name", name).put("members", arr)) }
        applyGroup(g, g.getLong("id"))
        return "g:${g.getLong("id")}"
    }

    suspend fun addMember(groupId: Long, username: String) {
        applyGroup(authed { it.post("/api/groups/$groupId/members", JSONObject().put("username", username.trim().lowercase())) }, groupId)
    }

    suspend fun removeMember(groupId: Long, username: String) {
        applyGroup(authed { it.delete("/api/groups/$groupId/members/$username") }, groupId)
    }

    suspend fun refreshGroups() {
        val list = authed { it.get("/api/groups") }.getJSONArray("groups")
        val ids = mutableSetOf<Long>()
        for (i in 0 until list.length()) {
            val g = list.getJSONObject(i)
            ids += g.getLong("id")
            applyGroup(g, g.getLong("id"))
        }
        dao.groupConversations().mapNotNull { it.groupId }.filter { it !in ids }.forEach { applyGroup(null, it) }
    }

    private suspend fun applyGroup(g: JSONObject?, groupId: Long) {
        val id = "g:$groupId"
        val old = dao.conversation(id)
        val members = g?.getJSONArray("members")?.let { a -> (0 until a.length()).map { a.getString(it) } }
        if (g == null || members == null || me !in members) {
            old?.let { dao.upsertConversation(it.copy(removed = true)) }
            return
        }
        dao.upsertConversation((old ?: Conversation(id, g.getString("name"), groupId = groupId, lastTs = System.currentTimeMillis()))
            .copy(title = g.getString("name"), owner = g.getString("owner"), members = members.joinToString(","), removed = false))
    }

    private suspend fun recipients(conv: Conversation): Map<String, ByteArray> =
        if (conv.groupId == null) mapOf(conv.id.removePrefix("u:").let { it to boxKeyOf(it) })
        else conv.members.split(",").filter { it.isNotEmpty() && it != me }.associateWith { boxKeyOf(it) }

    // ================================================================ sending
    suspend fun sendText(convId: String, text: String) {
        if (text.isBlank()) return
        queue(convId, text.trim(), null)
    }

    suspend fun sendFile(convId: String, uri: Uri) {
        val cr = app.contentResolver
        var name = "file"; var size = 0L
        cr.query(uri, null, null, null, null)?.use { c ->
            if (c.moveToFirst()) {
                name = c.getString(c.getColumnIndexOrThrow(OpenableColumns.DISPLAY_NAME)) ?: name
                size = c.getLong(c.getColumnIndexOrThrow(OpenableColumns.SIZE))
            }
        }
        if (size > MAX_FILE) { events.tryEmit("Файл больше 50 МБ"); return }
        val data = cr.openInputStream(uri)?.use { it.readBytes() } ?: return
        val (blob, key) = E2E.encryptFile(data)
        val blobId = authed { it.uploadBlob(blob) }
        val meta = JSONObject().put("blob", blobId).put("k", E2E.b64(key)).put("name", name)
            .put("mime", cr.getType(uri) ?: "application/octet-stream").put("size", data.size)
        queue(convId, "📎 $name", meta.toString())
    }

    private suspend fun queue(convId: String, text: String, fileJson: String?) {
        val cid = UUID.randomUUID().toString()
        val now = System.currentTimeMillis()
        val m = Message(convId = convId, sender = me, text = text, fileJson = fileJson, ts = now,
            outgoing = true, status = Message.STATUS_SENDING, cid = cid)
        dao.insertMessage(m)
        dao.conversation(convId)?.let { dao.upsertConversation(it.copy(lastText = text, lastTs = now)) }
        transmit(m)
    }

    suspend fun retry(m: Message) {
        m.cid?.let { dao.setStatus(it, Message.STATUS_SENDING, 0) }
        transmit(m.copy(status = Message.STATUS_SENDING))
    }

    /** Encrypts and hands the message to the relay; stays "sending" while offline. */
    private suspend fun transmit(m: Message) {
        val socket = ws ?: return
        if (!online.value) return
        val conv = dao.conversation(m.convId) ?: return
        val payload = JSONObject().put("cid", m.cid).put("g", conv.groupId ?: JSONObject.NULL)
        if (m.fileJson != null) {
            val f = JSONObject(m.fileJson)
            payload.put("t", "file")
            f.keys().forEach { payload.put(it, f.get(it)) }
        } else payload.put("t", "text").put("text", m.text)

        val keys = try { recipients(conv) } catch (e: KeyChangedException) {
            dao.setStatus(m.cid!!, Message.STATUS_FAILED, 0)
            events.tryEmit("⚠ Ключи ${e.username} изменились. Сверьте код безопасности и подтвердите.")
            return
        }
        if (keys.isEmpty()) { dao.setStatus(m.cid!!, Message.STATUS_SENT, 0); return }
        val env = E2E.encrypt(account.value!!.keys, keys, payload.toString().toByteArray())
        socket.send(JSONObject().put("op", "send").put("cid", m.cid)
            .put("group_id", conv.groupId ?: JSONObject.NULL)
            .put("body", env.body).put("keys", JSONObject(env.keys)).toString())
    }

    // ================================================================ receiving
    private suspend fun onIncoming(j: JSONObject) {
        val serverId = j.getLong("id")
        val from = j.getString("from")
        val groupId = if (j.isNull("group_id")) null else j.getLong("group_id")
        val ts = j.getLong("ts") * 1000
        val convId = if (groupId != null) "g:$groupId" else "u:$from"

        var contact = dao.contact(from) ?: refreshContact(from)
        var plain = contact?.let { E2E.decrypt(account.value!!.keys, E2E.unb64(it.boxPk), j.getString("body"), j.getString("key")) }
        if (plain == null) {                       // maybe the sender re-installed: check the server's current keys
            contact = runCatching { refreshContact(from) }.getOrNull()
            val pk = contact?.pendingBoxPk
            if (pk != null && E2E.decrypt(account.value!!.keys, E2E.unb64(pk), j.getString("body"), j.getString("key")) != null) {
                // Not acked: the server keeps it and redelivers once the user accepts the new keys.
                events.tryEmit("⚠ $from пишет с новыми ключами. Сообщение придёт после проверки кода безопасности.")
                return
            }
        }
        val p = plain?.let { runCatching { JSONObject(String(it)) }.getOrNull() }
        val claimedGroup = p?.let { if (it.isNull("g")) null else it.optLong("g") }
        if (p == null || claimedGroup != groupId) {
            insertIncoming(convId, from, "⚠ Не удалось расшифровать сообщение", null, ts, "in:$from:srv$serverId", groupId)
            ack(serverId); return
        }
        val dedupe = "in:$from:${p.optString("cid", "srv$serverId")}"
        if (dao.byCid(dedupe) == null) {
            val (text, file) = if (p.optString("t") == "file") {
                val meta = JSONObject().put("blob", p.getString("blob")).put("k", p.getString("k"))
                    .put("name", p.optString("name", "file")).put("mime", p.optString("mime")).put("size", p.optLong("size"))
                "📎 ${meta.getString("name")}" to meta.toString()
            } else p.optString("text") to null
            insertIncoming(convId, from, text, file, ts, dedupe, groupId)
        }
        ack(serverId)
    }

    private suspend fun insertIncoming(convId: String, from: String, text: String, file: String?, ts: Long, cid: String, groupId: Long?) {
        if (dao.conversation(convId) == null) {
            if (groupId != null) runCatching { refreshGroups() }
            if (dao.conversation(convId) == null) dao.upsertConversation(Conversation(convId, if (groupId != null) "Группа" else from, groupId = groupId))
        }
        dao.insertMessage(Message(convId = convId, sender = from, text = text, fileJson = file, ts = ts, outgoing = false, cid = cid))
        val c = dao.conversation(convId)!!
        val active = activeConversation == convId
        dao.upsertConversation(c.copy(lastText = text, lastTs = ts, unread = if (active) 0 else c.unread + 1))
        if (!active) Notifier.newMessage(app, c.title)
    }

    private fun ack(id: Long) {
        ws?.send(JSONObject().put("op", "ack").put("ids", JSONArray(listOf(id))).toString())
    }

    // ================================================================ attachments
    suspend fun openAttachment(m: Message): File {
        val meta = JSONObject(m.fileJson!!)
        val dir = File(app.cacheDir, "att").apply { mkdirs() }
        val safeName = meta.getString("name").replace(Regex("[^\\p{L}\\p{N}._ -]"), "_").take(80)
        val out = File(dir, "${m.id}_$safeName")
        if (!out.exists()) {
            val blob = authed { it.downloadBlob(meta.getString("blob")) }
            val data = E2E.decryptFile(blob, E2E.unb64(meta.getString("k"))) ?: throw SecurityException("Файл повреждён или подменён")
            out.writeBytes(data)
        }
        return out
    }

    fun clearAttachmentCache() { File(app.cacheDir, "att").deleteRecursively() }
}
