package com.mainnetwallet.mmagent.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp

/**
 * Add a withdrawal recipient.
 *
 * Adding needs a step-up token, and the server applies a cooling period before
 * the address can be used. The address format and checksum are validated by the
 * server, which is the only place that can judge them correctly.
 */
@Composable
fun AllowlistAddScreen(
    error: String?,
    busy: Boolean,
    onAdd: (label: String, address: String, network: String, asset: String, password: String) -> Unit,
    onBack: () -> Unit,
) {
    var label by remember { mutableStateOf("") }
    var address by remember { mutableStateOf("") }
    var network by remember { mutableStateOf("") }
    var asset by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .padding(16.dp),
    ) {
        SectionHeader("Add a recipient", "A new address waits out a cooling period")
        ErrorNote(error)
        Spacer(Modifier.height(10.dp))
        OutlinedTextField(
            value = label,
            onValueChange = { label = it },
            label = { Text("Label") },
            placeholder = { Text("cold storage") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = address,
            onValueChange = { address = it },
            label = { Text("Address") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedTextField(
                value = network,
                onValueChange = { network = it },
                label = { Text("Network") },
                placeholder = { Text("base") },
                singleLine = true,
                modifier = Modifier.weight(1f),
            )
            OutlinedTextField(
                value = asset,
                onValueChange = { asset = it },
                label = { Text("Asset") },
                placeholder = { Text("USDC") },
                singleLine = true,
                modifier = Modifier.weight(1f),
            )
        }
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = password,
            onValueChange = { password = it },
            label = { Text("Password (never stored)") },
            visualTransformation = PasswordVisualTransformation(),
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(12.dp))
        ConfirmButton(
            label = "Add recipient",
            enabled = label.isNotBlank() && address.isNotBlank() && network.isNotBlank() &&
                asset.isNotBlank() && password.isNotBlank(),
            busy = busy,
            onClick = { onAdd(label, address, network, asset, password) },
        )
        Spacer(Modifier.height(8.dp))
        Text(
            "This is the final recipient, not any intermediate bridge address.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(12.dp))
        ConfirmButton("Back", enabled = true, onClick = onBack)
    }
}
