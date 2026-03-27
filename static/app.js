/* Dota 2 Match Analyzer — client-side SSE handling and rendering */

'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
let totalPlayers  = 10;
let playersLoaded = 0;

// ── DOM references ───────────────────────────────────────────────────────────
const statusDot    = document.getElementById('statusDot');
const statusText   = document.getElementById('statusText');
const waitingState = document.getElementById('waitingState');
const matchView    = document.getElementById('matchView');
const listYours    = document.getElementById('listYours');
const listEnemy    = document.getElementById('listEnemy');

// ── SSE Connection ───────────────────────────────────────────────────────────
let evtSource = null;

function connectSSE() {
  if (evtSource) evtSource.close();

  evtSource = new EventSource('/stream');

  evtSource.onopen = function () {
    setStatus('connected', 'Connected — waiting for match…');
  };

  evtSource.onerror = function () {
    setStatus('error', 'Reconnecting…');
    evtSource.close();
    setTimeout(connectSSE, 3000);
  };

  evtSource.onmessage = function (e) {
    let msg;
    try { msg = JSON.parse(e.data); } catch (_) { return; }
    handleEvent(msg.type, msg.data);
  };
}

// ── Event dispatcher ─────────────────────────────────────────────────────────
function handleEvent(type, data) {
  switch (type) {
    case 'status':         handleStatus(data);        break;
    case 'match_detected': handleMatchDetected(data); break;
    case 'player_data':    handlePlayerData(data);    break;
  }
}

// ── Event handlers ───────────────────────────────────────────────────────────
function handleStatus(data) {
  if (data.status === 'waiting') {
    setStatus('connected', 'Connected — waiting for match…');
    showWaiting();
  }
}

function handleMatchDetected(data) {
  playersLoaded = 0;
  const nTeam  = data.n_teammates || 0;
  const nEnemy = data.n_enemies   || 0;
  totalPlayers = nTeam + nEnemy || 10;
  setStatus('connected', `Fetching player data… (0/${totalPlayers})`);

  listYours.innerHTML = '';
  listEnemy.innerHTML = '';
  for (let i = 0; i < nTeam;  i++) listYours.insertAdjacentHTML('beforeend', renderSkeleton());
  for (let i = 0; i < nEnemy; i++) listEnemy.insertAdjacentHTML('beforeend', renderSkeleton());

  showMatchView();
}

function handlePlayerData(data) {
  playersLoaded += 1;

  const isTeammate = data.team === 'teammate';
  const list       = isTeammate ? listYours : listEnemy;
  const skeleton   = list.querySelector('.skeleton-card');
  const html       = renderPlayerCard(data, isTeammate ? 'radiant' : 'dire');

  if (skeleton) {
    const tmp = document.createElement('div');
    tmp.innerHTML = html;
    list.replaceChild(tmp.firstElementChild, skeleton);
  } else {
    list.insertAdjacentHTML('beforeend', html);
  }

  if (playersLoaded >= totalPlayers) {
    setStatus('connected', 'Match ready ✓');
  } else {
    setStatus('connected', `Fetching player data… (${playersLoaded}/${totalPlayers})`);
  }
}

// ── UI helpers ────────────────────────────────────────────────────────────────
function setStatus(state, text) {
  statusDot.className = 'status-dot';
  if (state === 'connected') statusDot.classList.add('connected');
  if (state === 'error')     statusDot.classList.add('error');
  statusText.textContent = text;
}

function showWaiting() {
  waitingState.style.display = 'flex';
  matchView.style.display    = 'none';
}

function showMatchView() {
  waitingState.style.display = 'none';
  matchView.style.display    = 'block';
}

// ── Skeleton ──────────────────────────────────────────────────────────────────
function renderSkeleton() {
  return `
  <div class="skeleton-card">
    <div class="skeleton-header">
      <div class="skeleton-avatar"></div>
      <div class="skeleton-header-info">
        <div class="skeleton-line w-60 h-16"></div>
        <div class="skeleton-line w-40"></div>
      </div>
    </div>
  </div>`;
}

// ── Player Card ───────────────────────────────────────────────────────────────
function renderPlayerCard(player, teamColor) {
  const profile   = player.profile   || {};
  const matches   = player.matches   || [];
  const topHeroes = player.top_heroes || [];
  const isYou     = player.is_you === true;
  const name      = escapeHtml(profile.name       || 'Unknown');
  const rank      = escapeHtml(profile.rank       || 'Unranked');
  const role      = escapeHtml(player.main_role   || '');
  const dotabuff  = escapeHtml(profile.dotabuff_url || '#');
  const opendota  = escapeHtml(profile.opendota_url || '#');
  const avatarUrl = profile.avatar || '';

  const rankClass = getRankClass(rank);
  const youBadge  = isYou ? `<span class="badge-you">YOU</span>` : '';

  // Ranked win rate
  const rankedPct   = player.ranked_pct   != null ? player.ranked_pct   : null;
  const rankedTotal = player.ranked_total != null ? player.ranked_total : 0;
  const rankedStr   = rankedPct !== null
    ? `${rankedPct}% (${rankedTotal} ranked)`
    : '';

  // Avatar
  let avatarHtml;
  if (avatarUrl) {
    const safe = escapeHtml(avatarUrl);
    const init = escapeHtml((name.charAt(0) || '?').toUpperCase());
    avatarHtml = `
      <img class="avatar-img" src="${safe}" alt="${name}"
        onerror="this.style.display='none';this.nextElementSibling.style.display='flex';" />
      <div class="avatar-fallback" style="display:none">${init}</div>`;
  } else {
    const init = escapeHtml((name.charAt(0) || '?').toUpperCase());
    avatarHtml = `<div class="avatar-fallback">${init}</div>`;
  }

  // Win rate (from recent matches)
  const wins   = matches.filter(m => m.result === 'Win').length;
  const losses = matches.filter(m => m.result === 'Loss').length;
  const total  = wins + losses;
  const pct    = total > 0 ? Math.round((wins / total) * 100) : null;
  const pctStr = pct !== null ? `${pct}%` : 'N/A';
  const wrClass = pct !== null && pct < 50 ? 'wr-low' : 'wr-high';
  const barFillClass = pct !== null && pct < 50 ? 'winrate-bar-fill low' : 'winrate-bar-fill';
  const barWidth = pct !== null ? pct : 0;

  // Hero preview icons (5 small icons shown in summary)
  const previewIcons = topHeroes.slice(0, 5).map(h =>
    h.img
      ? `<img class="hero-preview-icon" src="${escapeHtml(h.img)}" alt="${escapeHtml(h.hero || '')}" onerror="this.style.display='none'">`
      : ''
  ).join('');

  const heroCount   = topHeroes.length;
  const matchCount  = matches.length;

  return `
  <div class="player-card${isYou ? ' player-card--you' : ''}">

    <!-- ── Always-visible header ───────────────────────────────────────── -->
    <div class="card-header">
      <div class="avatar-wrap">${avatarHtml}</div>

      <div class="card-meta">
        <div class="card-name-row">
          <span class="player-name" title="${name}">${name}</span>
          ${youBadge}
          <span class="badge-rank ${rankClass}">${rank}</span>
          ${role ? `<span class="badge-role">${escapeHtml(role)}</span>` : ''}
        </div>
        <div class="card-wr-row">
          <span class="wr-record">${wins}W ${losses}L</span>
          <span class="wr-pct ${wrClass}">${pctStr}</span>
          <div class="winrate-bar-track"><div class="${barFillClass}" style="width:${barWidth}%"></div></div>
          ${rankedStr ? `<span class="ranked-wr">${escapeHtml(rankedStr)}</span>` : ''}
        </div>
      </div>

      <div class="card-actions">
        <a class="link-btn link-btn--dotabuff" href="${dotabuff}" target="_blank" rel="noopener noreferrer" title="Dotabuff">DB</a>
        <a class="link-btn link-btn--opendota"  href="${opendota}"  target="_blank" rel="noopener noreferrer" title="OpenDota">OD</a>
      </div>
    </div>

    <!-- ── Top Heroes (collapsed) ──────────────────────────────────────── -->
    <details>
      <summary>
        <span class="summary-label">Top Heroes${heroCount ? ` (${heroCount})` : ''}</span>
        <div class="summary-right">
          ${previewIcons ? `<div class="hero-preview-row">${previewIcons}</div>` : ''}
        </div>
      </summary>
      <div class="heroes-grid">
        ${topHeroes.map(h => renderHeroCard(h)).join('')}
        ${topHeroes.length === 0 ? '<p class="no-data">No hero data.</p>' : ''}
      </div>
    </details>

    <!-- ── Recent Matches (collapsed) ─────────────────────────────────── -->
    <details>
      <summary>
        <span class="summary-label">Recent Matches${matchCount ? ` (${matchCount})` : ''}</span>
      </summary>
      ${renderMatchesTable(matches)}
    </details>

  </div>`;
}

// ── Hero Card (grid item) ─────────────────────────────────────────────────────
function renderHeroCard(hero) {
  const name   = escapeHtml(hero.hero    || 'Unknown');
  const imgUrl = hero.img    || '';
  const games  = hero.games  || 0;
  const wins   = hero.wins   || 0;
  const pct    = games > 0 ? Math.round((wins / games) * 100) : null;
  const wrText = pct !== null ? `${pct}%` : 'N/A';
  const wrCls  = pct !== null && pct < 50 ? 'hc-wr loss' : 'hc-wr win';

  const imgHtml = imgUrl
    ? `<img class="hc-img" src="${escapeHtml(imgUrl)}" alt="${name}" onerror="this.src=''">`
    : `<div class="hc-img hc-img--empty"></div>`;

  return `
  <div class="hero-card">
    ${imgHtml}
    <div class="hc-info">
      <span class="hc-name" title="${name}">${name}</span>
      <span class="${wrCls}">${wrText}</span>
      <span class="hc-games">${games}g</span>
    </div>
  </div>`;
}

// ── Recent Matches Table ──────────────────────────────────────────────────────
function renderMatchesTable(matches) {
  if (!matches || matches.length === 0) {
    return '<p class="no-data">No recent matches found.</p>';
  }

  const rows = matches.map(m => {
    const hero     = escapeHtml(m.hero     || '');
    const imgUrl   = m.hero_img || '';
    const result   = m.result   || '';
    const kda      = `${m.kills || 0}/${m.deaths || 0}/${m.assists || 0}`;
    const duration = escapeHtml(m.duration  || '');
    const mode     = escapeHtml(m.game_mode || '');
    const date     = escapeHtml(m.date      || '');
    const resClass = result === 'Win' ? 'result-win' : 'result-loss';

    const heroImg = imgUrl
      ? `<img class="hero-thumb" src="${escapeHtml(imgUrl)}" alt="${hero}" onerror="this.src=''">`
      : '';

    return `
    <tr>
      <td>${date}</td>
      <td><div class="match-hero-cell">${heroImg}<span>${hero}</span></div></td>
      <td class="${resClass}">${escapeHtml(result)}</td>
      <td class="kda-cell">${kda}</td>
      <td>${duration}</td>
      <td>${mode}</td>
    </tr>`;
  }).join('');

  return `
  <table class="matches-table">
    <thead>
      <tr>
        <th>Date</th><th>Hero</th><th>Result</th>
        <th>K/D/A</th><th>Duration</th><th>Mode</th>
      </tr>
    </thead>
    <tbody>${rows}</tbody>
  </table>`;
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function getRankClass(rankStr) {
  if (!rankStr) return '';
  const l = rankStr.toLowerCase();
  if (l.startsWith('herald'))   return 'herald';
  if (l.startsWith('guardian')) return 'guardian';
  if (l.startsWith('crusader')) return 'crusader';
  if (l.startsWith('archon'))   return 'archon';
  if (l.startsWith('legend'))   return 'legend';
  if (l.startsWith('ancient'))  return 'ancient';
  if (l.startsWith('divine'))   return 'divine';
  if (l.startsWith('immortal')) return 'immortal';
  return '';
}

// ── Boot ──────────────────────────────────────────────────────────────────────
connectSSE();
