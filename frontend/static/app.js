// The browser is an instructor observer. Bots receive separate private snapshots;
// this socket displays public state and may start hands without taking a seat.
const socket = new WebSocket(`${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`);

const stateEls = {
  phase: document.getElementById('phase-badge'),
  pot: document.getElementById('pot-display'),
  players: document.getElementById('players'),
  community: document.getElementById('community-cards'),
  dealer: document.getElementById('dealer-mark'),
  status: document.getElementById('status-message'),
  startButton: document.getElementById('start-hand-button'),
  seriesButton: document.getElementById('start-series-button'),
  seriesStatus: document.getElementById('series-status'),
  stackChart: document.getElementById('stack-chart')
};

stateEls.startButton.addEventListener('click', () => {
  socket.send(JSON.stringify({ type: 'start_hand' }));
});

stateEls.seriesButton.addEventListener('click', () => {
  socket.send(JSON.stringify({ type: 'start_series' }));
});

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[character]));
}

// cardsprites.png is a 13-col x 4-row grid: cols are rank 2..A left to right,
// rows are hearts, clubs, diamonds, spades top to bottom.
const SPRITE_SUIT_ROW = { hearts: 0, clubs: 1, diamonds: 2, spades: 3 };

function card(cardValue) {
  const hidden = cardValue.label === '??';
  if (hidden) {
    return '<div class="card hidden">?</div>';
  }
  const col = cardValue.rank - 2;
  const row = SPRITE_SUIT_ROW[cardValue.suit];
  return `<div class="card" style="--col:${col}; --row:${row}" aria-label="${escapeHtml(cardValue.label)}"></div>`;
}

function renderStackChart(players) {
  const maxStack = Math.max(1, ...players.map((player) => player.stack));
  stateEls.stackChart.innerHTML = players.map((player) => {
    const width = Math.max(2, Math.round((player.stack / maxStack) * 100));
    return `<div class="stack-row" title="${escapeHtml(player.name)}: $${player.stack}">
      <span class="stack-name">${escapeHtml(player.name)}</span>
      <span class="stack-track"><span class="stack-fill" style="width: ${width}%"></span></span>
      <span class="stack-value">$${player.stack}</span>
    </div>`;
  }).join('') || '<div class="empty-chart">Waiting for bots…</div>';
}

function renderState(snapshot) {
  const canStart = Boolean(snapshot.can_start);
  const series = snapshot.series;
  const seriesRunning = Boolean(series?.active);
  stateEls.startButton.disabled = !canStart || snapshot.phase !== 'WAITING' || seriesRunning;
  stateEls.seriesButton.disabled = !canStart || snapshot.phase !== 'WAITING' || seriesRunning;
  stateEls.seriesStatus.textContent = series
    ? (seriesRunning ? `10-hand series: ${series.completed_hands} of ${series.total_hands} completed` : `10-hand series complete: ${series.completed_hands} hands played`)
    : 'Single-hand mode';
  stateEls.phase.textContent = snapshot.phase;
  stateEls.pot.textContent = `Pot: $${snapshot.pot}`;
  stateEls.dealer.textContent = `Dealer button: ${snapshot.dealer || 'None'}`;
  const community = (snapshot.community_cards || []).map(card).join('');
  const placeholders = Array.from({ length: Math.max(0, 5 - (snapshot.community_cards || []).length) }, () => '<div class="card hidden">?</div>').join('');
  stateEls.community.innerHTML = community + placeholders;
  const players = snapshot.players || [];
  stateEls.players.innerHTML = players.map((player) => {
    const turn = snapshot.current_turn === player.id;
    const state = player.folded ? 'Folded' : (player.all_in ? 'All-in' : (turn ? 'Acting' : 'Waiting'));
    return `<div class="player-seat ${turn ? 'current-turn' : ''} ${player.folded ? 'folded' : ''}">
      <div class="player-name">${escapeHtml(player.name)}${snapshot.dealer === player.id ? '<span class="dealer-chip">D</span>' : ''}</div>
      <div class="player-hole">${(player.hole_cards || []).map(card).join('')}</div>
      <div class="player-stack">Stack: $${player.stack}</div>
      <div class="player-bet">Street bet: $${player.bet}</div>
      <div class="player-state">${state}</div>
    </div>`;
  }).join('');
  renderStackChart(players);
  const action = snapshot.last_action;
  if (action?.type === 'payout') {
    stateEls.status.textContent = `Hand complete — ${Object.keys(action.payouts || {}).join(', ') || action.winners.join(', ')} collected the pot.`;
  } else if (snapshot.current_turn) {
    stateEls.status.textContent = `Hand ${snapshot.hand_number}: waiting for ${snapshot.current_turn}.`;
  } else if (snapshot.phase === 'WAITING' && !canStart) {
    stateEls.status.textContent = 'Need at least two connected players with chips to start a hand.';
  } else {
    stateEls.status.textContent = 'Waiting for bots to join or start a hand.';
  }
}

socket.addEventListener('open', () => socket.send(JSON.stringify({ type: 'observe' })));
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data);
  if (message.type === 'state') renderState(message.payload);
  if (message.type === 'error') stateEls.status.textContent = message.message;
});
socket.addEventListener('close', () => { stateEls.status.textContent = 'Disconnected from the table display.'; });
