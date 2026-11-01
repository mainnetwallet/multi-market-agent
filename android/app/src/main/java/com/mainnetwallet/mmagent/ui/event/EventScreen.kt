package com.mainnetwallet.mmagent.ui.event

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.key
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.mainnetwallet.mmagent.data.EventUiState
import com.mainnetwallet.mmagent.ui.ErrorNote

/**
 * One event, drawn from the layout the server chose (`view.layout`), never from a category name. An unknown layout
 * falls back to the generic list, so any opened market renders. Reading [EventUiState.rev] is what redraws it when
 * a patch changes the store in place.
 */
@Composable
fun EventScreen(
    state: EventUiState,
    onRange: (String) -> Unit,
    onBack: () -> Unit,
    onTrade: OnTrade,
) {
    val view = state.view
    Column(Modifier.fillMaxSize()) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            TextButton(onClick = onBack) { Text("‹ Back") }
            Text(
                view?.text("title").orEmpty(),
                style = MaterialTheme.typography.titleSmall,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
        }
        LiveBadge(state.indicator, Modifier.padding(horizontal = 16.dp))
        Spacer(Modifier.height(8.dp))
        when {
            view == null && state.loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
            view == null -> Column(Modifier.padding(16.dp)) { ErrorNote(state.error ?: "Could not load this event.") }
            else -> Column(
                Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                // `rev` is read here so Compose redraws after an in-place patch.
                key(state.rev) {
                    when (view.text("layout")) {
                        "game_3way", "game_2way" -> GameScreen(view, state.range, onRange, onTrade)
                        "bracket_ladder" -> BracketScreen(view, state.range, onRange, onTrade)
                        else -> GenericScreen(view, state.range, onRange, onTrade)
                    }
                }
                state.error?.let { ErrorNote(it) }
            }
        }
    }
}
