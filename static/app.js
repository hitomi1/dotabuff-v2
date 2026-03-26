/* Dota 2 Match Analyzer — client-side SSE handling and rendering */

'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
let totalPlayers  = 10;
let playersLoaded = 0;

// ── DOM references ───────────────────────────────────────────────────────────
const statusDot   = document.getElementById('statusDot');
const statusText  = document.getElementById('statusText');
const waitingState = document.getElementById('waitingState');
const matchView   = document.getElementById('matchView');
const listYours   = document.getElementById('listYours');
const listEnemy   = document.getElementById('listEnemy');

// ── SSE Connection ───────────────────────────────────────────────────────────
let evtSource = null;

function connectSSE() {
  if (evtSource) {
    evtSource.close();
  }

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
    try {
      msg = JSON.parse(e.data);
    } catch (_) {
      return;
    }
    handleEvent(msg.type, msg.data);
  };
}

// ── Event dispatcher ─────────────────────────────────────────────────────────
function handleEvent(type, data) {
  switch (type) {
    case 'status':
      handleStatus(data);
      break;
    case 'match_detected':
      handleMatchDetected(data);
      break;
    case 'player_data':
      handlePlayerData(data);
      break;
    default:
      break;
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
  const nTeam   = data.n_teammates || 0;
  const nEnemy  = data.n_enemies   || 0;
  totalPlayers  = nTeam + nEnemy || 10;
  setStatus('connected', `Fetching player data… (0/${totalPlayers})`);

  // Clear columns and populate with skeletons
  listYours.innerHTML = '';
  listEnemy.innerHTML = '';
  for (let i = 0; i < nTeam; i++) {
    listYours.insertAdjacentHTML('beforeend', renderSkeleton());
  }
  for (let i = 0; i < nEnemy; i++) {
    listEnemy.insertAdjacentHTML('beforeend', renderSkeleton());
  }

  showMatchView();
}

function handlePlayerData(data) {
  playersLoaded += 1;

  const isTeammate = data.team === 'teammate';
  const list = isTeammate ? listYours : listEnemy;
  const teamColor = isTeammate ? 'radiant' : 'dire';

  // Find first skeleton and replace it
  const skeleton = list.querySelector('.skeleton-card');
  const html = renderPlayerCard(data, teamColor);

  if (skeleton) {
    const temp = document.createElement('div');
    temp.innerHTML = html;
    const card = temp.firstElementChild;
    list.replaceChild(card, skeleton);
  } else {
    list.insertAdjacentHTML('beforeend', html);
  }

  if (playersLoaded >= totalPlayers) {
    setStatus('connected', 'Match ready ✓');
  } else {
    setStatus('connected', `Fetching player data… (${playersLoaded}/${totalPlayers})`);
  }
}

// ── UI state helpers ─────────────────────────────────────────────────────────
function setStatus(state, text) {
  statusDot.className = 'status-dot';
  if (state === 'connected') statusDot.classList.add('connected');
  if (state === 'error')     statusDot.classList.add('error');
  statusText.textContent = text;
}

function showWaiting() {
  waitingState.style.display = 'flex';
  matchView.style.display = 'none';
}

function showMatchView() {
  waitingState.style.display = 'none';
  matchView.style.display = 'block';
}

// ── Rendering ────────────────────────────────────────────────────────────────

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
    <div class="skeleton-line w-80"></div>
    <div class="skeleton-line w-100"></div>
    <div class="skeleton-line w-60"></div>
  </div>`;
}

/**
 * Render a full player card.
 * @param {object} player  — the player_data payload from SSE
 * @param {string} teamColor — 'radiant' or 'dire'
 * @returns {string} HTML string
 */
function renderPlayerCard(player, teamColor) {
  const profile    = player.profile || {};
  const matches    = player.matches || [];
  const topHeroes  = player.top_heroes || [];
  const isYou      = player.is_you === true;
  const name       = escapeHtml(profile.name || 'Unknown');
  const rank       = escapeHtml(profile.rank || 'Unranked');
  const role       = escapeHtml(player.main_role || 'Unknown');
  const dotabuff   = escapeHtml(profile.dotabuff_url || '#');
  const opendota   = escapeHtml(profile.opendota_url || '#');
  const avatarUrl  = profile.avatar || '';

  const rankClass  = getRankClass(rank);
  const youBadge   = isYou ? `<span class="badge-you">YOU</span>` : '';
  const rankBadge  = `<span class="badge-rank ${rankClass}">${rank}</span>`;
  const roleBadge  = `<span class="badge-role">${role}</span>`;

  // Avatar
  let avatarHtml;
  if (avatarUrl) {
    const safeAvatar = escapeHtml(avatarUrl);
    const initial    = name.charAt(0) || '?';
    avatarHtml = `<img
      class="avatar-img"
      src="${safeAvatar}"
      alt="${name}"
      onerror="this.style.display='none';this.nextElementSibling.style.display='flex';"
    /><div class="avatar-fallback" style="display:none">${escapeHtml(initial.toUpperCase())}</div>`;
  } else {
    const initial = name.charAt(0) || '?';
    avatarHtml = `<div class="avatar-fallback">${escapeHtml(initial.toUpperCase())}</div>`;
  }

  // Win-rate calculation from matches
  const wins   = matches.filter(m => m.result === 'Win').length;
  const losses = matches.filter(m => m.result === 'Loss').length;
  const total  = wins + losses;
  const pct    = total > 0 ? Math.round((wins / total) * 100) : 0;
  const pctStr = total > 0 ? `${pct}%` : 'N/A';
  const barClass = pct < 50 ? 'winrate-bar-fill low' : 'winrate-bar-fill';
  const barWidth = total > 0 ? pct : 0;

  const winrateSummary = `
  <div class="winrate-summary">
    <div class="winrate-label">
      <span class="winrate-record">${wins}W ${losses}L &mdash; ${pctStr}</span>
      <span class="winrate-pct">${pctStr}</span>
    </div>
    <div class="winrate-bar-track">
      <div class="${barClass}" style="width:${barWidth}%"></div>
    </div>
  </div>`;

  return `
  <div class="player-card">
    <div class="card-header">
      <div class="avatar-wrap">${avatarHtml}</div>
      <div class="card-meta">
        <div class="card-name-row">
          <span class="player-name" title="${name}">${name}</span>
          ${youBadge}
        </div>
        <div class="card-badges">
          ${rankBadge}
          ${roleBadge}
        </div>
      </div>
    </div>

    <div class="profile-links">
      <a class="link-btn link-btn--dotabuff" href="${dotabuff}" target="_blank" rel="noopener noreferrer">
        &#9632; Dotabuff
      </a>
      <a class="link-btn link-btn--opendota" href="${opendota}" target="_blank" rel="noopener noreferrer">
        &#9632; OpenDota
      </a>
    </div>

    ${winrateSummary}

    <details open>
      <summary>Top Heroes</summary>
      <div class="heroes-list">
        ${topHeroes.map(h => renderHeroRow(h)).join('')}
        ${topHeroes.length === 0 ? '<p style="color:var(--text-faint);font-size:12px;padding:4px 0">No hero data available.</p>' : ''}
      </div>
    </details>

    <details>
      <summary>Recent Matches</summary>
      ${renderMatchesTable(matches)}
    </details>
  </div>`;
}

function renderHeroRow(hero) {
  const name    = escapeHtml(hero.hero || 'Unknown');
  const imgUrl  = hero.img || '';
  const games   = hero.games || 0;
  const wins    = hero.wins || 0;
  const winrate = hero.winrate || 'N/A';
  const pct     = games > 0 ? Math.round((wins / games) * 100) : 0;

  const imgHtml = imgUrl
    ? `<img class="hero-row-img" src="${escapeHtml(imgUrl)}" alt="${name}" onerror="this.src=''">`
    : `<div class="hero-row-img"></div>`;

  return `
  <div class="hero-row">
    ${imgHtml}
    <span class="hero-row-name" title="${name}">${name}</span>
    <div class="hero-bar-wrap">
      <div class="hero-bar-track">
        <div class="hero-bar-fill" style="width:${pct}%"></div>
      </div>
    </div>
    <div class="hero-stats">
      <span class="hero-winpct">${escapeHtml(winrate)}</span>
      <span class="hero-games">${games}g</span>
    </div>
  </div>`;
}

function renderMatchesTable(matches) {
  if (!matches || matches.length === 0) {
    return '<p style="color:var(--text-faint);font-size:12px;padding:6px 0">No recent matches found.</p>';
  }

  const rows = matches.map(m => {
    const hero     = escapeHtml(m.hero || '');
    const imgUrl   = m.hero_img || '';
    const result   = m.result || '';
    const kda      = `${m.kills || 0}/${m.deaths || 0}/${m.assists || 0}`;
    const duration = escapeHtml(m.duration || '');
    const mode     = escapeHtml(m.game_mode || '');
    const date     = escapeHtml(m.date || '');
    const resClass = result === 'Win' ? 'result-win' : 'result-loss';

    const heroImgHtml = imgUrl
      ? `<img class="hero-thumb" src="${escapeHtml(imgUrl)}" alt="${hero}" onerror="this.src=''">`
      : '';

    return `
    <tr>
      <td>${date}</td>
      <td><div class="match-hero-cell">${heroImgHtml}<span>${hero}</span></div></td>
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
        <th>Date</th>
        <th>Hero</th>
        <th>Result</th>
        <th>K/D/A</th>
        <th>Duration</th>
        <th>Mode</th>
      </tr>
    </thead>
    <tbody>${rows}</tbody>
  </table>`;
}

// ── Helper functions ─────────────────────────────────────────────────────────

/**
 * Escape HTML special characters to prevent XSS.
 * @param {*} str
 * @returns {string}
 */
function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Map a rank string to its CSS class name.
 * @param {string} rankStr  e.g. "Ancient 3", "Immortal"
 * @returns {string}
 */
function getRankClass(rankStr) {
  if (!rankStr) return '';
  const lower = rankStr.toLowerCase();
  if (lower.startsWith('herald'))   return 'herald';
  if (lower.startsWith('guardian')) return 'guardian';
  if (lower.startsWith('crusader')) return 'crusader';
  if (lower.startsWith('archon'))   return 'archon';
  if (lower.startsWith('legend'))   return 'legend';
  if (lower.startsWith('ancient'))  return 'ancient';
  if (lower.startsWith('divine'))   return 'divine';
  if (lower.startsWith('immortal')) return 'immortal';
  return '';
}

// ── Boot ─────────────────────────────────────────────────────────────────────
connectSSE();
