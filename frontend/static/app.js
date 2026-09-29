// The browser is an instructor observer. Bots receive separate private snapshots;
// this socket displays public state and may start hands without taking a seat.
const socket = new WebSocket(`${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`);

const stateEls = {
  phase: document.getElementById('phase-badge'),
  pot: document.getElementById('pot-display'),
  players: document.getElementById('players'),
  community: document.getElementById('community-cards'),
  tablePhase: document.getElementById('table-phase-text'),
  tablePot: document.getElementById('table-pot-text'),
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
    return '<div class="card hidden" aria-label="face-down card"></div>';
  }
  const col = cardValue.rank - 2;
  const row = SPRITE_SUIT_ROW[cardValue.suit];
  return `<div class="card" style="--col:${col}; --row:${row}" aria-label="${escapeHtml(cardValue.label)}"></div>`;
}

// Tracks each player's most recent decision so it can render above their
// name. The engine only exposes a single table-wide last_action, so this
// persists across renders and resets whenever a new hand deals.
const lastActionByPlayer = {};

function describeAction(action) {
  switch (action.type) {
    case 'fold':
      return { player: action.player, text: 'Folded' };
    case 'check':
      return { player: action.player, text: 'Checked' };
    case 'call':
      return { player: action.player, text: `Called $${action.amount}` };
    case 'raise':
      return { player: action.player, text: `Raised to $${action.target}` };
    default:
      return null;
  }
}

function updateLastActions(action) {
  if (!action) return;
  if (action.type === 'deal') {
    Object.keys(lastActionByPlayer).forEach((key) => delete lastActionByPlayer[key]);
    lastActionByPlayer[action.small_blind] = 'Posted small blind';
    lastActionByPlayer[action.big_blind] = 'Posted big blind';
    return;
  }
  const described = describeAction(action);
  if (described) lastActionByPlayer[described.player] = described.text;
}

// Leaderboard rows are kept as persistent DOM nodes (keyed by player id)
// rather than rebuilt every render, so animating `top` on rank changes
// actually produces a slide instead of a instant jump.
const STACK_ROW_HEIGHT = 48;
const stackRowEls = new Map();

function renderStackChart(players) {
  if (!players.length) {
    stateEls.stackChart.innerHTML = '<div class="empty-chart">Waiting for bots…</div>';
    stackRowEls.clear();
    return;
  }
  const stray = stateEls.stackChart.querySelector('.empty-chart');
  if (stray) stray.remove();

  const ranked = [...players].sort((a, b) => b.stack - a.stack);
  const seen = new Set();
  ranked.forEach((player, index) => {
    seen.add(player.id);
    let row = stackRowEls.get(player.id);
    if (!row) {
      row = document.createElement('div');
      row.className = 'stack-row';
      row.innerHTML = '<span class="stack-rank"></span><span class="stack-name"></span><span class="stack-value"></span>';
      row.style.top = `${index * STACK_ROW_HEIGHT}px`;
      stateEls.stackChart.appendChild(row);
      stackRowEls.set(player.id, row);
    }
    row.title = `${player.name}: $${player.stack}`;
    row.querySelector('.stack-rank').textContent = `${index + 1}`;
    row.querySelector('.stack-name').textContent = player.name;
    row.querySelector('.stack-value').textContent = `$${player.stack}`;
    row.style.top = `${index * STACK_ROW_HEIGHT}px`;
  });
  for (const [id, row] of stackRowEls) {
    if (!seen.has(id)) {
      row.remove();
      stackRowEls.delete(id);
    }
  }
  stateEls.stackChart.style.height = `${ranked.length * STACK_ROW_HEIGHT}px`;
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
  stateEls.tablePhase.textContent = snapshot.phase;
  stateEls.tablePot.textContent = `Pot: $${snapshot.pot}`;
  updateLastActions(snapshot.last_action);
  const community = (snapshot.community_cards || []).map(card).join('');
  const placeholders = Array.from({ length: Math.max(0, 5 - (snapshot.community_cards || []).length) }, () => '<div class="card hidden" aria-label="face-down card"></div>').join('');
  stateEls.community.innerHTML = community + placeholders;
  const players = snapshot.players || [];
  stateEls.players.innerHTML = players.map((player) => {
    const turn = snapshot.current_turn === player.id;
    const state = player.folded ? 'Folded' : (player.all_in ? 'All-in' : (turn ? 'Acting' : 'Waiting'));
    return `<div class="player-seat ${turn ? 'current-turn' : ''} ${player.folded ? 'folded' : ''}">
      <div class="player-last-action">${escapeHtml(lastActionByPlayer[player.id] || '')}</div>
      <div class="player-name"><span class="player-name-text">${escapeHtml(player.name)}</span>${snapshot.dealer === player.id ? '<span class="dealer-chip">D</span>' : ''}</div>
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
