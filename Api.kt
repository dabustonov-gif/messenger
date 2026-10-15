package com.securemsg.app.net

import android.content.Context
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.io.IOException
import java.security.KeyStore
import java.security.cert.CertificateFactory
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class ApiException(val code: String, val http: Int) : IOException("$http $code")

/**
 * HTTPS + WebSocket client. If assets/server_ca.crt exists (the root of your Caddy "tls internal" CA),
 * ONLY that CA is trusted — the app then refuses any other certificate, even a valid public one.
 * Without the file the system trust store is used (e.g. Tailscale certs / Let's Encrypt).
 */
class Api(context: Context, baseUrl: String) {
    val base = baseUrl.trimEnd('/')
    var token: String? = null

    val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .pingInterval(25, TimeUnit.SECONDS)
        .apply { pinnedTrust(context)?.let { (ctx, tm) -> sslSocketFactory(ctx.socketFactory, tm) } }
        .build()

    init {
        require(base.startsWith("https://")) { "Адрес сервера должен начинаться с https://" }
    }

    private fun pinnedTrust(context: Context): Pair<SSLContext, X509TrustManager>? {
        val input = runCatching { context.assets.open("server_ca.crt") }.getOrNull() ?: return null
        val ks = KeyStore.getInstance(KeyStore.getDefaultType()).apply { load(null) }
        input.use { CertificateFactory.getInstance("X.509").generateCertificates(it) }
            .forEachIndexed { i, cert -> ks.setCertificateEntry("ca$i", cert) }
        val tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply { init(ks) }
        val tm = tmf.trustManagers.filterIsInstance<X509TrustManager>().first()
        return SSLContext.getInstance("TLS").apply { init(null, arrayOf(tm), null) } to tm
    }

    private fun req(path: String) = Request.Builder().url(base + path).apply {
        token?.let { header("Authorization", "Bearer $it") }
    }

    private suspend fun exec(r: Request): ByteArray = withContext(Dispatchers.IO) {
        http.newCall(r).execute().use { resp ->
            val bytes = resp.body?.bytes() ?: ByteArray(0)
            if (!resp.isSuccessful) {
                val code = runCatching { JSONObject(String(bytes)).getString("error") }.getOrDefault("http_${resp.code}")
                throw ApiException(code, resp.code)
            }
            bytes
        }
    }

    private val jsonType = "application/json".toMediaType()

    suspend fun get(path: String) = JSONObject(String(exec(req(path).get().build())))
    suspend fun post(path: String, body: JSONObject = JSONObject()) =
        JSONObject(String(exec(req(path).post(body.toString().toRequestBody(jsonType)).build())))
    suspend fun delete(path: String) = JSONObject(String(exec(req(path).delete().build())))

    suspend fun uploadBlob(data: ByteArray): String =
        JSONObject(String(exec(req("/api/blobs").post(data.toRequestBody("application/octet-stream".toMediaType())).build())))
            .getString("blob_id")

    suspend fun downloadBlob(id: String): ByteArray = exec(req("/api/blobs/$id").get().build())

    fun openSocket(listener: WebSocketListener): WebSocket =
        http.newWebSocket(Request.Builder().url(base.replaceFirst("https://", "wss://") + "/ws").build(), listener)
}
