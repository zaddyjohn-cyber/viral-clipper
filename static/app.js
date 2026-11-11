const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = { job: null, picked: new Set(), poll: null, exporting: false };

const fmt = s => {
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(x).padStart(2, '0')}`
           : `${m}:${String(x).padStart(2, '0')}`;
};
const scoreColor = n => n >= 75 ? 'var(--good)' : n >= 50 ? 'var(--mid)' : 'var(--low)';
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function toast(title, body, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.innerHTML = `<i></i><div><strong>${esc(title)}</strong>${body ? `<p>${esc(body)}</p>` : ''}</div>`;
  el.addEventListener('click', () => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 280);
  });
  $('#toasts').append(el);
  if (kind !== 'err') {
    setTimeout(() => {
      el.classList.add('out');
      setTimeout(() => el.remove(), 280);
    }, 6000);
  }
}

function show(view) {
  $$('.view').forEach(v => v.classList.toggle('is-active', v.dataset.view === view));
  $$('.nav-item').forEach(n => n.classList.toggle('is-active', n.dataset.view === view));
  $('.main').scrollTop = 0;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' }, ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || data.detail || `Request failed (${res.status})`);
  return data;
}

/* ───────── preflight ───────── */
async function preflight() {
  try {
    const p = await api('/api/preflight');
    $$('.health-row').forEach(r => {
      r.classList.toggle('ok', !!p[r.dataset.k]);
      r.classList.toggle('bad', !p[r.dataset.k]);
    });
    return p;
  } catch { return {}; }
}

/* ───────── settings ───────── */
let cfg = null;

async function loadSettings() {
  cfg = await api('/api/config');
  $('#clipLen').value = cfg.clip_len;
  $('#maxClips').value = cfg.max_clips;
  $('#cookies').value = cfg.cookies;
  $('#cookiesFile').value = cfg.cookies_file || '';
  $('#outdir').value = cfg.outdir;
  $('#vertical').checked = !!cfg.vertical;
  $('#blurPad').checked = !!cfg.blur_pad;
  $('#speakerTrack').checked = !!cfg.speaker_track;
  $('#wordCaptions').checked = !!cfg.word_captions;
  $('#hookCard').checked = !!cfg.hook_card;
  paintProviders();
  paintTranscribers();
  paintCaptions();
}

function paintCaptions() {
  $('#captionStyles').innerHTML = Object.entries(cfg.caption_styles).map(([id, c]) => `
    <button type="button" class="prov ${id === cfg.captions ? 'is-on' : ''}" data-cap="${id}">
      <strong>${esc(c.label)}</strong>
      <span>${esc(c.blurb)}</span>
    </button>`).join('');
}

$('#captionStyles').addEventListener('click', e => {
  const btn = e.target.closest('[data-cap]');
  if (!btn) return;
  cfg.captions = btn.dataset.cap;
  paintCaptions();
});

function paintTranscribers() {
  $('#transcribers').innerHTML = Object.entries(cfg.transcribers).map(([id, t]) => `
    <button type="button" class="prov ${id === cfg.transcriber ? 'is-on' : ''} ${!t.needs_key || cfg.keys_set.groq ? 'has-key' : ''}" data-trans="${id}">
      <strong>${esc(t.label)}</strong>
      <span>${esc(t.blurb)}</span>
      <em>${!t.needs_key ? 'No key needed' : cfg.keys_set.groq ? 'Key saved' : 'Needs a key'}</em>
    </button>`).join('');

  const t = cfg.transcribers[cfg.transcriber];
  const current = cfg.transcriber === 'groq' ? cfg.groq_model : cfg.whisper_model;
  $('#transModel').innerHTML = t.models
    .map(m => `<option value="${m}" ${m === current ? 'selected' : ''}>${m}</option>`).join('');

  $('#groqKeyField').hidden = !t.needs_key;
  if (t.needs_key) {
    const has = cfg.keys_set.groq;
    $('#groqKeyState').textContent = has
      ? 'Key saved. Leave blank to keep it.'
      : `Not set. Get one at ${t.keys_url}`;
    $('#groqKeyState').style.color = has ? 'var(--good)' : 'var(--low)';
  }
}

$('#transcribers').addEventListener('click', e => {
  const btn = e.target.closest('[data-trans]');
  if (!btn) return;
  cfg.transcriber = btn.dataset.trans;
  $('#groqKey').value = '';
  paintTranscribers();
});

function paintProviders() {
  $('#providers').innerHTML = Object.entries(cfg.providers).map(([id, p]) => `
    <button type="button" class="prov ${id === cfg.provider ? 'is-on' : ''} ${cfg.keys_set[id] ? 'has-key' : ''}" data-prov="${id}">
      <strong>${esc(p.label)}</strong>
      <span>${esc(p.model)}</span>
      <em>${cfg.keys_set[id] ? 'Key saved' : 'No key'}</em>
    </button>`).join('');

  const p = cfg.providers[cfg.provider];
  $('#provName').textContent = p.label;
  $('#apiKey').placeholder = p.key_prefix + '...';
  const has = cfg.keys_set[cfg.provider];
  $('#keyState').textContent = has
    ? 'Key saved. Leave blank to keep it.'
    : `Not set. Get one at ${p.keys_url}`;
  $('#keyState').style.color = has ? 'var(--good)' : 'var(--low)';
}

$('#providers').addEventListener('click', e => {
  const btn = e.target.closest('[data-prov]');
  if (!btn) return;
  cfg.provider = btn.dataset.prov;
  $('#apiKey').value = '';
  paintProviders();
});

$('#settingsForm').addEventListener('submit', async e => {
  e.preventDefault();
  const body = {
    provider: cfg.provider,
    transcriber: cfg.transcriber,
    captions: cfg.captions,
    clip_len: +$('#clipLen').value,
    max_clips: +$('#maxClips').value,
    cookies: $('#cookies').value,
    cookies_file: $('#cookiesFile').value.trim(),
    outdir: $('#outdir').value.trim(),
    vertical: $('#vertical').checked,
    blur_pad: $('#blurPad').checked,
    speaker_track: $('#speakerTrack').checked,
    word_captions: $('#wordCaptions').checked,
    hook_card: $('#hookCard').checked,
  };
  if (cfg.transcriber === 'groq') body.groq_model = $('#transModel').value;
  else body.whisper_model = $('#transModel').value;

  const keys = {};
  const key = $('#apiKey').value.trim();
  if (key) keys[cfg.provider] = key;
  const gkey = $('#groqKey').value.trim();
  if (gkey) keys.groq = gkey;
  if (Object.keys(keys).length) body.keys = keys;

  try {
    await api('/api/config', { method: 'POST', body: JSON.stringify(body) });
    $('#apiKey').value = '';
    $('#groqKey').value = '';
    await loadSettings();
    await preflight();
    const note = $('#savedNote');
    note.hidden = false;
    setTimeout(() => { note.hidden = true; }, 2200);
  } catch (err) {
    toast('Could not save', err.message, 'err');
  }
});

/* ───────── analyse ───────── */
$('#urlForm').addEventListener('submit', async e => {
  e.preventDefault();
  const url = $('#urlInput').value.trim();
  if (!url) return;

  $('#goBtn').disabled = true;
  try {
    const job = await api('/api/analyze', {
      method: 'POST', body: JSON.stringify({ url }),
    });
    state.job = job;
    state.picked.clear();
    show('processing');
    paintSteps(job);
    startPolling();
  } catch (err) {
    toast('Could not start', err.message, 'err');
    if (/api key/i.test(err.message)) show('settings');
  } finally {
    $('#goBtn').disabled = false;
  }
});

function startPolling() {
  clearInterval(state.poll);
  state.poll = setInterval(async () => {
    if (!state.job) return;
    try {
      const job = await api(`/api/job/${state.job.id}`);
      state.job = job;

      if (job.status === 'error') {
        clearInterval(state.poll);
        paintRunPill(job);
        toast('Could not finish', job.error, 'err');
        show('create');
        return;
      }

      if (job.status === 'cancelled') {
        clearInterval(state.poll);
        paintRunPill(job);
        toast('Run stopped', 'Nothing was saved from that one.');
        show('create');
        return;
      }

      if (job.stage === 'ready') {
        paintSteps(job);
        paintResults(job);
        if (!state.exporting) {
          clearInterval(state.poll);
          state.poll = setInterval(refreshExports, 1500);
        }
        return;
      }

      paintSteps(job);
    } catch (err) {
      clearInterval(state.poll);
      state.job = null;
      paintRunPill(null);
      show('create');
      toast('That run is gone', 'The server restarted. Start it again.', 'err');
    }
  }, 1200);
}

async function refreshExports() {
  if (!state.job) return;
  try {
    const job = await api(`/api/job/${state.job.id}`);
    const wasExporting = state.exporting;
    state.job = job;
    const busy = job.clips.some(c => c.exporting);
    state.exporting = busy;
    paintGrid(job.clips);
    paintRunPill(job);
    if (job.export_error) {
      state.exporting = false;
      clearInterval(state.poll);
      toast('Could not fetch the video', job.export_error, 'err');
      return;
    }
    if (wasExporting && !busy) {
      const n = job.clips.filter(c => c.exported).length;
      toast('Clips exported', `${n} file${n === 1 ? '' : 's'} written to your output folder.`, 'ok');
      clearInterval(state.poll);
    }
  } catch { clearInterval(state.poll); }
}

function paintSteps(job) {
  const order = job.stages;
  const at = order.indexOf(job.stage);
  const pct = Math.round(job.percent || 0);

  $$('#steps li').forEach(li => {
    const i = order.indexOf(li.dataset.stage);
    const done = i < at || job.stage === 'ready';
    const active = i === at && job.stage !== 'ready';
    li.classList.toggle('is-done', done);
    li.classList.toggle('is-active', active);
    li.querySelector('.bar b').style.width = active ? pct + '%' : '0';
    li.querySelector('u').textContent = active ? pct + '%' : '';
  });

  $('#procMsg').textContent = job.message || '';
  $('#procTitle').textContent = job.meta?.title
    ? job.meta.title.slice(0, 70) : 'Working on your video';
  $('#procElapsed').textContent = fmt(job.elapsed || 0) + ' elapsed';
  paintRunPill(job);
}

const STAGE_WORD = { download: 'Downloading', transcribe: 'Transcribing',
                     score: 'Scoring', ready: 'Done' };

function paintRunPill(job) {
  const pill = $('#runPill');
  const live = job && (job.status === 'running' || job.exporting);
  pill.hidden = !live;
  if (!live) return;
  const indet = job.exporting && !job.fetching_video;
  const pct = Math.round(indet ? 100 : job.percent || 0);
  $('#runPillStage').textContent = job.fetching_video ? 'Fetching video'
    : job.exporting ? 'Exporting' : STAGE_WORD[job.stage] || 'Working';
  $('#runPillPct').textContent = indet ? '' : pct + '%';
  $('#runPillBar').style.width = pct + '%';
  $('#runPillMsg').textContent = job.message || '';
}

/* ───────── results ───────── */
function paintResults(job) {
  $('#resultsEmpty').hidden = true;
  $('#resultsWrap').hidden = false;
  $('#navBadge').hidden = false;
  $('#navBadge').textContent = job.clips.length;

  $('#vidPlatform').textContent = job.platform;
  $('#vidTitle').textContent = job.meta?.title || 'Untitled video';
  const bits = [job.meta?.uploader, job.meta?.duration ? fmt(job.meta.duration) : null]
    .filter(Boolean);
  $('#vidMeta').textContent = bits.join('  ·  ');

  const scores = job.clips.map(c => c.score);
  const best = Math.max(...scores, 0);
  const avg = scores.length ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : 0;
  const strong = scores.filter(s => s >= 75).length;

  $('#stats').innerHTML = [
    ['Moments found', job.clips.length, 'scored windows', 'var(--accent-2)'],
    ['Best score', best + '%', 'top clip', scoreColor(best)],
    ['Average score', avg + '%', 'across all clips', scoreColor(avg)],
    ['Strong picks', strong, 'scoring 75% or higher', strong ? 'var(--good)' : 'var(--faint)'],
  ].map(([l, v, s, c]) => `
    <div class="stat" style="--sc:${c}">
      <div class="stat-label">${l}</div>
      <div class="stat-value">${v}</div>
      <div class="stat-sub">${s}</div>
    </div>`).join('');

  if (!state.picked.size) job.clips.filter(c => c.score >= 70).forEach(c => state.picked.add(c.n));
  paintGrid(job.clips);
  show('library');
}

/* The grid is rebuilt from scratch on every poll, which would throw away a
   clip someone is in the middle of watching. The player is lifted out
   first and put back into its card afterwards, still at the same moment. */
function keepPlaying(paint) {
  const live = playingN === null ? null : $('.clip-media video');
  const at = live ? live.currentTime : 0;
  const running = live ? !live.paused : false;
  if (live) live.remove();

  paint();

  if (!live) return;
  const media = $(`.clip[data-n="${playingN}"] .clip-media`);
  if (!media) { playingN = null; return; }
  media.appendChild(live);
  media.classList.add('is-playing');
  $(`[data-watch="${playingN}"]`)?.classList.add('is-playing');
  if (Math.abs(live.currentTime - at) > 0.3) live.currentTime = at;
  if (running) live.play().catch(() => {});
}

function paintGrid(clips) {
  keepPlaying(() => drawGrid(clips));
  updateSel();
}

function drawGrid(clips) {
  const C = 2 * Math.PI * 21;
  // Analysis only pulls the audio, so until the video is here a moment can
  // be heard but not seen. The first export fetches it, once, for all clips.
  const local = !!(state.job && state.job.has_video);
  $('#clipGrid').innerHTML = clips.map((c, i) => {
    const col = scoreColor(c.score);
    const off = C * (1 - c.score / 100);
    const picked = state.picked.has(c.n);

    let foot;
    if (c.exporting) {
      foot = `<div class="clip-state is-working">
          <span class="mini-spin"></span>Cutting ${c.percent || 0}%
          <div class="bar clip-bar"><b style="width:${c.percent || 0}%"></b></div>
        </div>`;
    } else if (c.export_error) {
      foot = `<div class="clip-state is-bad">Export failed</div>`;
    } else if (c.exported) {
      foot = `<div class="clip-state">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>
          Saved</div>
        <a class="btn btn-ghost" href="/api/clip/${state.job.id}/${c.n}" download>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v10M7.5 10 12 14.5 16.5 10M5 19h14"/></svg>
          Download</a>`;
    } else {
      foot = `<button class="btn btn-accent" data-export="${c.n}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v10M7.5 10 12 14.5 16.5 10M5 19h14"/></svg>
          Export this clip</button>`;
    }

    return `
    <article class="clip ${picked ? 'is-picked' : ''}" style="--sc:${col};animation-delay:${i * 45}ms" data-n="${c.n}">
      <div class="clip-media">
        ${c.thumb ? `<img src="${c.thumb}" alt="" loading="lazy">` : ''}
        <button class="clip-pick" data-pick="${c.n}" aria-label="Select clip">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>
        </button>
        <div class="ring">
          <svg viewBox="0 0 50 50">
            <circle class="bg" cx="25" cy="25" r="21"/>
            <circle class="fg" cx="25" cy="25" r="21"
              stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"/>
          </svg>
          <b>${c.score}</b>
        </div>
        <span class="clip-time">${fmt(c.start)} - ${fmt(c.end)}</span>
        <span class="clip-len">${Math.round(c.duration)}s</span>
      </div>
      <div class="clip-body">
        <div class="clip-title">${esc(c.title)}</div>
        ${c.hook_card ? `<div class="clip-card" title="Burned over the first seconds">${esc(c.hook_card)}</div>` : ''}
        ${c.hook ? `<div class="clip-hook">${esc(c.hook)}</div>` : ''}
        ${c.reason ? `<div class="clip-reason">${esc(c.reason)}</div>` : ''}
        <div class="clip-listen">
          <button class="listen" data-watch="${c.n}" title="${local
            ? 'See this moment' : 'Hear this moment. The picture arrives with your first export'}">
            <svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13l11-6.5z"/></svg>
            ${local ? 'Watch' : 'Listen'}
          </button>
          ${c.text ? `<button class="readmore" data-read="${c.n}">Read it</button>` : ''}
        </div>
        ${c.text ? `<div class="clip-text" id="txt-${c.n}" hidden>${esc(c.text)}</div>` : ''}
        ${c.tags?.length ? `<div class="clip-tags">${c.tags.map(t => `<span>${esc(t)}</span>`).join('')}</div>` : ''}
      </div>
      <div class="clip-foot">${foot}</div>
    </article>`;
  }).join('');
}

function updateSel() {
  const n = state.picked.size;
  $('#selCount').textContent = `${n} selected`;
  $('#exportBtn').disabled = n === 0 || state.exporting;

  // Only cut clips can be downloaded, so the button appears once the
  // selection holds some, and says how many it will actually fetch.
  const ready = downloadable();
  const btn = $('#downloadBtn');
  btn.hidden = ready.length === 0;
  btn.lastChild.textContent = ready.length === 1
    ? ' Download clip' : ` Download ${ready.length} clips`;
}

function downloadable() {
  const clips = (state.job && state.job.clips) || [];
  return clips.filter(c => c.exported && state.picked.has(c.n));
}

/* ───────── interactions ───────── */
$('#clipGrid').addEventListener('click', async e => {
  const pick = e.target.closest('[data-pick]');
  if (pick) {
    const n = +pick.dataset.pick;
    state.picked.has(n) ? state.picked.delete(n) : state.picked.add(n);
    pick.closest('.clip').classList.toggle('is-picked', state.picked.has(n));
    updateSel();
    return;
  }
  const one = e.target.closest('[data-export]');
  if (one) { exportClips([+one.dataset.export]); return; }

  const read = e.target.closest('[data-read]');
  if (read) {
    const box = $('#txt-' + read.dataset.read);
    box.hidden = !box.hidden;
    read.textContent = box.hidden ? 'Read it' : 'Hide';
    return;
  }

  const play = e.target.closest('[data-watch]');
  if (play) { watch(+play.dataset.watch, play); return; }
});

let playingN = null;
let audio = null;

function stopWatching() {
  if (audio) { audio.pause(); audio = null; }
  $$('.clip-media video').forEach(v => { v.pause(); v.remove(); });
  $$('.clip-media').forEach(m => m.classList.remove('is-playing'));
  $$('[data-watch]').forEach(b => b.classList.remove('is-playing', 'is-loading'));
  playingN = null;
}

/* The moment plays where its thumbnail is, so the card stays put and the
   score, hook and transcript sit beside what you are looking at.

   This is a seek into the downloaded source rather than a render, so it
   starts at once. Before the source is here there is only the audio
   analysis pulled, and that plays instead. */
function watch(n, btn) {
  if (playingN === n) { stopWatching(); return; }
  stopWatching();

  const clip = (state.job.clips || []).find(c => c.n === n);
  const media = btn.closest('.clip').querySelector('.clip-media');
  if (!clip || !media) return;
  playingN = n;

  if (!state.job.has_video) { hear(n, clip, btn); return; }

  const video = document.createElement('video');
  video.src = `/api/source/${state.job.id}#t=${clip.start.toFixed(2)}`;
  video.controls = true;
  video.autoplay = true;
  video.playsInline = true;
  video.addEventListener('ended', stopWatching);
  // The fragment start is a hint some browsers ignore, and none of them
  // know where the moment ends, so both ends are held here.
  video.addEventListener('loadedmetadata', () => {
    if (Math.abs(video.currentTime - clip.start) > 0.5) video.currentTime = clip.start;
  });
  video.addEventListener('timeupdate', () => {
    if (video.currentTime >= clip.end) { video.pause(); stopWatching(); }
  });
  video.addEventListener('error', () => {
    stopWatching();
    toast('Could not play that', 'The downloaded video is no longer on disk.', 'err');
  });

  media.appendChild(video);
  media.classList.add('is-playing');
  btn.classList.add('is-playing');
}

function hear(n, clip, btn) {
  btn.classList.add('is-playing');
  audio = new Audio(`/api/preview/${state.job.id}/${n}`);
  audio.addEventListener('ended', stopWatching);
  audio.play().catch(() => {
    toast('Could not play that', 'The audio for this run may have been cleared.', 'err');
    stopWatching();
  });
}

$('#selectAllBtn').addEventListener('click', () => {
  state.job?.clips.forEach(c => state.picked.add(c.n));
  paintGrid(state.job.clips);
});
$('#selectNoneBtn').addEventListener('click', () => {
  state.picked.clear();
  paintGrid(state.job.clips);
});
$('#exportBtn').addEventListener('click', () => exportClips([...state.picked]));

/* Browsers drop a burst of downloads fired in one go, so they are spaced
   out far enough that each one is treated as its own. */
$('#downloadBtn').addEventListener('click', () => {
  const ready = downloadable();
  if (!ready.length) return;
  ready.forEach((c, i) => setTimeout(() => {
    const a = document.createElement('a');
    a.href = `/api/clip/${state.job.id}/${c.n}`;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }, i * 400));
  toast(ready.length === 1 ? 'Downloading the clip' : `Downloading ${ready.length} clips`,
        'Your browser will put them wherever it keeps downloads.');
});

async function exportClips(nums) {
  if (!state.job || !nums.length) return;

  if (!state.job.has_video) {
    const mins = Math.round((state.job.meta?.duration || 0) / 60);
    const ok = confirm(
      `Analysis only downloaded the audio. Exporting needs the video, which is ` +
      `a one-time download of the full ${mins} minute source.

` +
      `It happens once, then every clip you export is instant. Continue?`);
    if (!ok) return;
  }

  try {
    await api(`/api/job/${state.job.id}/export`, {
      method: 'POST', body: JSON.stringify({ clips: nums }),
    });
    state.exporting = true;
    updateSel();
    toast('Exporting', `Cutting ${nums.length} clip${nums.length === 1 ? '' : 's'}. This takes a moment each.`);
    clearInterval(state.poll);
    state.poll = setInterval(refreshExports, 1500);
  } catch (err) {
    toast('Export failed', err.message, 'err');
  }
}

$('#cancelBtn').addEventListener('click', async () => {
  if (!state.job) return;
  $('#cancelBtn').disabled = true;
  try {
    await api(`/api/job/${state.job.id}/cancel`, { method: 'POST' });
  } catch (err) {
    toast('Could not stop it', err.message, 'err');
  } finally {
    $('#cancelBtn').disabled = false;
  }
});

$('#runPill').addEventListener('click', () => {
  show(state.job && state.job.stage === 'ready' ? 'library' : 'processing');
});

$('#revealBtn').addEventListener('click', async () => {
  try { await api('/api/reveal', { method: 'POST' }); }
  catch (err) { toast('Could not open folder', err.message, 'err'); }
});

$('#newRunBtn').addEventListener('click', () => {
  $('#urlInput').value = '';
  show('create');
  $('#urlInput').focus();
});

document.addEventListener('click', e => {
  const nav = e.target.closest('[data-view]');
  if (nav && (nav.classList.contains('nav-item') || nav.classList.contains('btn'))) {
    show(nav.dataset.view);
  }
});

/* ───────── boot ───────── */
async function rejoin() {
  try {
    const job = await api('/api/active');
    if (!job || !job.id) return;
    state.job = job;
    if (job.status === 'running') {
      paintSteps(job);
      show('processing');
      startPolling();
    } else if (job.stage === 'ready') {
      paintResults(job);
      show('create');
    }
  } catch { /* nothing in flight */ }
}

(async () => {
  await loadSettings();
  const p = await preflight();
  await rejoin();
  if (!p.api_key) {
    toast('Add your API key',
      `Open Settings and paste your ${p.provider_label || ''} key to get started.`);
  }
  if (!p.ffmpeg) toast('ffmpeg is missing', 'Install it and add it to PATH, then reload.', 'err');
  if (!p.yt_dlp) toast('yt-dlp is missing', 'Run: pip install yt-dlp', 'err');
})();
