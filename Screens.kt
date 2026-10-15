package com.securemsg.app.ui

import android.content.ActivityNotFoundException
import android.content.Intent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.AttachFile
import androidx.compose.material.icons.filled.Group
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Person
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Badge
import androidx.compose.material3.Button
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SmallFloatingActionButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import com.securemsg.app.Repo
import com.securemsg.app.data.Message
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

private fun err(e: Throwable) = when (e.message) {
    "403 bad_invite" -> "Неверный или использованный инвайт-код"
    "409 username_taken" -> "Имя занято"
    "400 bad_username" -> "Имя: 3–32 символа, латиница, цифры, _"
    else -> e.message ?: e.toString()
}

// ==================================================================== setup
@Composable
fun SetupScreen() {
    var server by remember { mutableStateOf("https://") }
    var username by remember { mutableStateOf("") }
    var invite by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    Column(Modifier.fillMaxSize().padding(24.dp).imePadding(), verticalArrangement = Arrangement.Center) {
        Icon(Icons.Default.Lock, null, Modifier.size(48.dp), tint = MaterialTheme.colorScheme.primary)
        Spacer(Modifier.height(12.dp))
        Text("Защищённый мессенджер", style = MaterialTheme.typography.headlineSmall)
        Text("Ключи шифрования создаются на этом телефоне и не покидают его.",
            style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(24.dp))
        OutlinedTextField(server, { server = it.trim() }, label = { Text("Адрес сервера") },
            placeholder = { Text("https://192.168.1.50") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri))
        OutlinedTextField(username, { username = it.lowercase().filter { c -> c.isLetterOrDigit() || c == '_' } },
            label = { Text("Имя пользователя") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(invite, { invite = it.trim() }, label = { Text("Инвайт-код") },
            singleLine = true, modifier = Modifier.fillMaxWidth())
        error?.let { Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(top = 8.dp)) }
        Spacer(Modifier.height(16.dp))
        Button(
            enabled = !busy && username.length >= 3 && invite.isNotEmpty() && server.length > 8,
            onClick = {
                busy = true; error = null
                scope.launch {
                    runCatching { Repo.register(server, username, invite) }.onFailure { error = err(it) }
                    busy = false
                }
            },
            modifier = Modifier.fillMaxWidth(),
        ) { Text(if (busy) "Создаю ключи…" else "Зарегистрироваться") }
    }
}

// ==================================================================== chat list
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatListScreen(onOpen: (String) -> Unit) {
    val convs by remember { Repo.dao.conversations() }.collectAsState(emptyList())
    val online by Repo.online.collectAsState()
    var menu by remember { mutableStateOf(false) }
    var dialog by remember { mutableStateOf<String?>(null) }   // "direct" | "group" | "wipe" | "invite:<code>"
    val scope = rememberCoroutineScope()

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("Чаты")
                        Text(if (online) "@${Repo.me} · в сети" else "@${Repo.me} · подключение…",
                            style = MaterialTheme.typography.labelSmall,
                            color = if (online) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.error)
                    }
                },
                actions = {
                    IconButton({ menu = true }) { Icon(Icons.Default.MoreVert, "Меню") }
                    DropdownMenu(menu, { menu = false }) {
                        DropdownMenuItem({ Text("Создать инвайт") }, {
                            menu = false
                            scope.launch {
                                runCatching { Repo.createInvite() }
                                    .onSuccess { dialog = "invite:$it" }
                                    .onFailure { Repo.events.tryEmit("Инвайты создаёт только администратор") }
                            }
                        })
                        DropdownMenuItem({ Text("Очистить кэш файлов") }, { menu = false; Repo.clearAttachmentCache() })
                        DropdownMenuItem({ Text("Удалить аккаунт с телефона", color = MaterialTheme.colorScheme.error) },
                            { menu = false; dialog = "wipe" })
                    }
                },
            )
        },
        floatingActionButton = {
            Column(horizontalAlignment = Alignment.End, verticalArrangement = Arrangement.spacedBy(12.dp)) {
                SmallFloatingActionButton({ dialog = "group" }) { Icon(Icons.Default.Group, "Новая группа") }
                SmallFloatingActionButton({ dialog = "direct" }) { Icon(Icons.Default.Person, "Новый чат") }
            }
        },
    ) { pad ->
        if (convs.isEmpty()) {
            Box(Modifier.fillMaxSize().padding(pad), contentAlignment = Alignment.Center) {
                Text("Пока нет чатов. Начните новый кнопкой справа внизу.", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        LazyColumn(Modifier.padding(pad)) {
            items(convs, key = { it.id }) { c ->
                Row(Modifier.fillMaxWidth().clickable { onOpen(c.id) }.padding(16.dp, 12.dp),
                    verticalAlignment = Alignment.CenterVertically) {
                    Avatar(c.title, group = c.groupId != null)
                    Column(Modifier.weight(1f).padding(start = 12.dp)) {
                        Text(c.title, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
                        Text(c.lastText, maxLines = 1, overflow = TextOverflow.Ellipsis,
                            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    if (c.unread > 0) Badge { Text("${c.unread}") }
                }
                HorizontalDivider()
            }
        }
    }

    when (val d = dialog) {
        "direct" -> InputDialog("Новый чат", listOf("Имя пользователя"), { dialog = null }) { v ->
            scope.launch {
                runCatching { Repo.startDirect(v[0]) }.onSuccess { dialog = null; onOpen(it) }
                    .onFailure { Repo.events.tryEmit(err(it)) }
            }
        }
        "group" -> InputDialog("Новая группа", listOf("Название", "Участники через запятую"), { dialog = null }) { v ->
            scope.launch {
                runCatching { Repo.createGroup(v[0], v[1].split(",")) }.onSuccess { dialog = null; onOpen(it) }
                    .onFailure { Repo.events.tryEmit(if (it.message?.contains("unknown_member") == true) "Кого-то из участников нет на сервере" else err(it)) }
            }
        }
        "wipe" -> AlertDialog(
            onDismissRequest = { dialog = null },
            title = { Text("Удалить аккаунт с телефона?") },
            text = { Text("Ключи и вся переписка будут стёрты без возможности восстановления. Для повторного входа понадобится новый инвайт и новое имя.") },
            confirmButton = { TextButton({ dialog = null; Repo.wipeEverything() }) { Text("Удалить", color = MaterialTheme.colorScheme.error) } },
            dismissButton = { TextButton({ dialog = null }) { Text("Отмена") } },
        )
        null -> {}
        else -> if (d.startsWith("invite:")) AlertDialog(
            onDismissRequest = { dialog = null },
            title = { Text("Инвайт-код") },
            text = { Text(d.removePrefix("invite:"), fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.titleLarge) },
            confirmButton = { TextButton({ dialog = null }) { Text("Готово") } },
        )
    }
}

@Composable
private fun Avatar(title: String, group: Boolean) {
    Box(Modifier.size(44.dp).background(MaterialTheme.colorScheme.primaryContainer, CircleShape), contentAlignment = Alignment.Center) {
        if (group) Icon(Icons.Default.Group, null, tint = MaterialTheme.colorScheme.onPrimaryContainer)
        else Text(title.take(1).uppercase(), color = MaterialTheme.colorScheme.onPrimaryContainer, fontWeight = FontWeight.Bold)
    }
}

@Composable
private fun InputDialog(title: String, fields: List<String>, onDismiss: () -> Unit, onOk: (List<String>) -> Unit) {
    var values by remember { mutableStateOf(fields.map { "" }) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = {
            Column {
                fields.forEachIndexed { i, label ->
                    OutlinedTextField(values[i], { v -> values = values.toMutableList().also { it[i] = v } },
                        label = { Text(label) }, singleLine = true)
                }
            }
        },
        confirmButton = { TextButton({ onOk(values) }, enabled = values.all { it.isNotBlank() }) { Text("OK") } },
        dismissButton = { TextButton(onDismiss) { Text("Отмена") } },
    )
}

// ==================================================================== chat
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(convId: String, onBack: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val conv by remember(convId) { Repo.dao.conversationFlow(convId) }.collectAsState(null)
    val messages by remember(convId) { Repo.dao.messages(convId) }.collectAsState(emptyList())
    val peer = convId.takeIf { it.startsWith("u:") }?.removePrefix("u:")
    val contact by remember(peer) { peer?.let { Repo.dao.contactFlow(it) } ?: flowOf(null) }.collectAsState(null)
    var text by remember { mutableStateOf("") }
    var showInfo by remember { mutableStateOf(false) }
    val listState = rememberLazyListState()

    DisposableEffect(convId) {
        Repo.activeConversation = convId
        onDispose { Repo.activeConversation = null }
    }
    LaunchedEffect(convId) {
        peer?.let { runCatching { Repo.refreshContact(it) } }
        Repo.dao.markRead(convId)
    }
    LaunchedEffect(messages.size) {
        if (messages.isNotEmpty()) listState.animateScrollToItem(messages.size - 1)
        Repo.dao.markRead(convId)
    }

    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) scope.launch { runCatching { Repo.sendFile(convId, uri) }.onFailure { Repo.events.tryEmit("Ошибка: ${err(it)}") } }
    }

    fun open(m: Message) = scope.launch {
        runCatching {
            val f = Repo.openAttachment(m)
            val uri = FileProvider.getUriForFile(ctx, ctx.packageName + ".files", f)
            val mime = runCatching { org.json.JSONObject(m.fileJson!!).getString("mime") }.getOrDefault("*/*")
            ctx.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, mime.ifEmpty { "*/*" })
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION))
        }.onFailure { Repo.events.tryEmit(if (it is ActivityNotFoundException) "Нет приложения для этого файла" else "Ошибка: ${err(it)}") }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                navigationIcon = { IconButton(onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Назад") } },
                title = {
                    Column(Modifier.clickable { showInfo = true }) {
                        Text(conv?.title ?: "", maxLines = 1)
                        val sub = when {
                            contact?.pendingSignPk != null -> "⚠ ключи изменились — нажмите"
                            contact?.verified == true -> "🔒 собеседник проверен"
                            peer != null -> "не проверен — нажмите, чтобы сверить код"
                            else -> "${conv?.members?.split(",")?.size ?: 0} участн."
                        }
                        Text(sub, style = MaterialTheme.typography.labelSmall,
                            color = if (contact?.pendingSignPk != null) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                },
            )
        },
    ) { pad ->
        Column(Modifier.fillMaxSize().padding(pad).imePadding()) {
            LazyColumn(Modifier.weight(1f).padding(horizontal = 8.dp), state = listState) {
                items(messages, key = { it.id }) { m -> Bubble(m, group = conv?.groupId != null, onOpen = { open(m) },
                    onRetry = { scope.launch { Repo.retry(m) } }) }
            }
            if (conv?.removed == true) {
                Text("Вы больше не участник этой группы", Modifier.padding(16.dp), color = MaterialTheme.colorScheme.error)
            } else Row(Modifier.fillMaxWidth().padding(8.dp), verticalAlignment = Alignment.CenterVertically) {
                IconButton({ picker.launch(arrayOf("*/*")) }) { Icon(Icons.Default.AttachFile, "Файл") }
                OutlinedTextField(text, { text = it }, Modifier.weight(1f), placeholder = { Text("Сообщение") }, maxLines = 5)
                IconButton({
                    val t = text; text = ""
                    scope.launch { Repo.sendText(convId, t) }
                }, enabled = text.isNotBlank()) { Icon(Icons.AutoMirrored.Filled.Send, "Отправить") }
            }
        }
    }

    if (showInfo) {
        if (peer != null) SafetyDialog(peer) { showInfo = false }
        else conv?.let { GroupDialog(it.groupId!!, it.owner, it.members.split(",").filter(String::isNotEmpty)) { showInfo = false } }
    }
}

private val timeFmt = SimpleDateFormat("dd.MM HH:mm", Locale.getDefault())

@Composable
private fun Bubble(m: Message, group: Boolean, onOpen: () -> Unit, onRetry: () -> Unit) {
    val mine = m.outgoing
    Box(Modifier.fillMaxWidth().padding(vertical = 3.dp), contentAlignment = if (mine) Alignment.CenterEnd else Alignment.CenterStart) {
        Column(
            Modifier.widthIn(max = 300.dp)
                .background(if (mine) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(14.dp))
                .clickable(enabled = m.fileJson != null || m.status == Message.STATUS_FAILED) { if (m.status == Message.STATUS_FAILED) onRetry() else onOpen() }
                .padding(10.dp, 6.dp),
        ) {
            if (group && !mine) Text(m.sender, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.primary)
            Text(m.text, textDecoration = null, fontWeight = if (m.fileJson != null) FontWeight.Medium else null)
            val status = when (m.status) {
                Message.STATUS_SENDING -> " · ⏳"
                Message.STATUS_FAILED -> " · ❗ нажмите, чтобы повторить"
                else -> if (mine) " · ✓" else ""
            }
            Text(timeFmt.format(Date(m.ts)) + status, style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun SafetyDialog(peer: String, onDismiss: () -> Unit) {
    val scope = rememberCoroutineScope()
    val c by remember(peer) { Repo.dao.contactFlow(peer) }.collectAsState(null)
    val contact = c ?: return
    AlertDialog(
        onDismissRequest = onDismiss,
        icon = { Icon(if (contact.pendingSignPk != null) Icons.Default.Warning else Icons.Default.Lock, null) },
        title = { Text(if (contact.pendingSignPk != null) "Ключи $peer изменились" else "Код безопасности") },
        text = {
            Column {
                if (contact.pendingSignPk != null) {
                    Text("Сервер сообщает о новых ключах. Так бывает, если $peer переустановил приложение, — или если кто-то пытается подменить собеседника. Свяжитесь с ним лично и сверьте новый код.")
                    Spacer(Modifier.height(8.dp))
                    Text(Repo.safetyNumber(contact.copy(signPk = contact.pendingSignPk)), fontFamily = FontFamily.Monospace)
                } else {
                    Text("Сверьте эти цифры с $peer при личной встрече или по телефону. Если они совпадают, переписку никто не подменяет.")
                    Spacer(Modifier.height(8.dp))
                    Text(Repo.safetyNumber(contact), fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.titleMedium)
                }
            }
        },
        confirmButton = {
            if (contact.pendingSignPk != null)
                TextButton({ scope.launch { Repo.acceptNewKeys(peer); onDismiss() } }) { Text("Код совпал, принять") }
            else
                TextButton({ scope.launch { Repo.setVerified(peer, !contact.verified); onDismiss() } }) {
                    Text(if (contact.verified) "Снять отметку" else "Код совпадает")
                }
        },
        dismissButton = { TextButton(onDismiss) { Text("Закрыть") } },
    )
}

@Composable
private fun GroupDialog(groupId: Long, owner: String?, members: List<String>, onDismiss: () -> Unit) {
    val scope = rememberCoroutineScope()
    var add by remember { mutableStateOf("") }
    val isOwner = owner == Repo.me
    fun run(block: suspend () -> Unit) = scope.launch { runCatching { block() }.onFailure { Repo.events.tryEmit(err(it)) } }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Участники") },
        text = {
            Column {
                members.forEach { m ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(m + if (m == owner) " (владелец)" else "", Modifier.weight(1f))
                        if (isOwner && m != Repo.me) TextButton({ run { Repo.removeMember(groupId, m) } }) { Text("Убрать") }
                    }
                }
                if (isOwner) Row(verticalAlignment = Alignment.CenterVertically) {
                    OutlinedTextField(add, { add = it }, Modifier.weight(1f), label = { Text("Добавить") }, singleLine = true)
                    TextButton({ val u = add; add = ""; run { Repo.addMember(groupId, u) } }, enabled = add.isNotBlank()) { Text("+") }
                }
            }
        },
        confirmButton = { TextButton(onDismiss) { Text("Закрыть") } },
        dismissButton = {
            if (!isOwner) TextButton({ run { Repo.removeMember(groupId, Repo.me) }; onDismiss() }) {
                Text("Выйти из группы", color = MaterialTheme.colorScheme.error)
            }
        },
    )
}
