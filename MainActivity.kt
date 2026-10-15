package com.securemsg.app.ui

import android.Manifest
import android.os.Bundle
import android.view.WindowManager
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import com.securemsg.app.Repo
import com.securemsg.app.service.RelayService

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // No screenshots, screen recording or previews in the app switcher.
        window.setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE)
        registerForActivityResult(ActivityResultContracts.RequestPermission()) {}
            .launch(Manifest.permission.POST_NOTIFICATIONS)

        setContent {
            MaterialTheme(colorScheme = if (isSystemInDarkTheme()) darkColorScheme() else lightColorScheme()) {
                val account by Repo.account.collectAsState()
                var openChat by rememberSaveable { mutableStateOf<String?>(null) }

                LaunchedEffect(Unit) {
                    Repo.events.collect { Toast.makeText(this@MainActivity, it, Toast.LENGTH_LONG).show() }
                }
                LaunchedEffect(account != null) {
                    if (account != null) RelayService.start(this@MainActivity) else RelayService.stop(this@MainActivity)
                }

                when {
                    account == null -> SetupScreen()
                    openChat != null -> {
                        BackHandler { openChat = null }
                        ChatScreen(openChat!!, onBack = { openChat = null })
                    }
                    else -> ChatListScreen(onOpen = { openChat = it })
                }
            }
        }
    }
}
