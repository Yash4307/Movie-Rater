/* MovieRater front-end. No dependencies. Every value that comes from the server or an API
   is escaped before it is put into HTML (the old script injected titles unescaped). */
(() => {
  'use strict';

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const cssUrl = (u) => esc(String(u || '').replace(/['()\\\s]/g, encodeURIComponent));
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || '';
  const NO_POSTER = '/static/img/no-poster.svg';
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  async function api(url, opts = {}) {
    const o = { credentials: 'same-origin', ...opts, headers: { 'X-CSRF-Token': csrf(), ...(opts.headers || {}) } };
    if (o.body && typeof o.body !== 'string') { o.body = JSON.stringify(o.body); o.headers['Content-Type'] = 'application/json'; }
    const r = await fetch(url, o);
    let data = null;
    try { data = await r.json(); } catch (e) { /* non-JSON response */ }
    if (r.status === 401) { location.href = '/login'; throw new Error('Please log in again'); }
    if (!r.ok) throw Object.assign(new Error((data && data.error) || 'Something went wrong'), { status: r.status });
    return data;
  }

  /* ---------- Toasts ---------- */
  function toast(msg, type = 'info') {
    const box = $('#toasts');
    if (!box) return;
    const t = document.createElement('div');
    t.className = `toast ${type}`;
    t.setAttribute('role', 'status');
    t.innerHTML = `<span>${esc(msg)}</span><button type="button" aria-label="Dismiss">&times;</button>`;
    t.querySelector('button').onclick = () => t.remove();
    box.appendChild(t);
    setTimeout(() => t.remove(), 4500);
  }
  $$('.toast').forEach((t) => setTimeout(() => t.remove(), 6000)); // server-side flash messages

  /* ---------- Broken posters fall back to a placeholder ---------- */
  document.addEventListener('error', (e) => {
    const el = e.target;
    if (el.tagName === 'IMG' && !el.dataset.fb) { el.dataset.fb = '1'; el.src = NO_POSTER; }
  }, true);

  /* ---------- Theme ---------- */
  const root = document.documentElement;
  const themeBtn = $('#themeToggle');
  const paintTheme = () => { if (themeBtn) themeBtn.innerHTML = `<i class="fas ${root.dataset.theme === 'light' ? 'fa-moon' : 'fa-sun'}"></i>`; };
  paintTheme();
  const loggedIn = !!document.body.dataset.user;
  if (themeBtn) themeBtn.onclick = async () => {
    const next = root.dataset.theme === 'light' ? 'dark' : 'light';
    root.dataset.theme = next;
    paintTheme();
    if (loggedIn) {            // saved on the account, so every device and every other user is unaffected
      try { await api('/api/theme', { method: 'POST', body: { theme: next } }); }
      catch (e) { toast('Could not save your theme', 'error'); }
    } else {                   // login / register pages: remembered for guests only
      try { localStorage.setItem('cr-theme-guest', next); } catch (e) { /* private mode */ }
    }
  };

  /* ---------- Sidebar (mobile) ---------- */
  const closeNav = () => document.body.classList.remove('nav-open');
  $('#menuBtn')?.addEventListener('click', () => document.body.classList.toggle('nav-open'));
  $('#scrim')?.addEventListener('click', closeNav);

  /* ---------- Password show/hide ---------- */
  $$('[data-toggle-password]').forEach((btn) => btn.addEventListener('click', () => {
    const input = document.getElementById(btn.dataset.togglePassword);
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    btn.innerHTML = `<i class="fas ${show ? 'fa-eye-slash' : 'fa-eye'}"></i>`;
    btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
  }));

  /* ---------- Views (hash based: #home, #discover, ...) ---------- */
  function initViews() {
    const views = $$('.view');
    if (!views.length) return;
    const names = views.map((v) => v.dataset.view);
    const show = () => {
      let n = location.hash.slice(1);
      if (!names.includes(n)) n = names[0];
      views.forEach((v) => v.classList.toggle('is-active', v.dataset.view === n));
      $$('[data-nav-view]').forEach((a) => a.classList.toggle('active', a.dataset.navView === n));
      closeNav();
      window.scrollTo(0, 0);
      document.dispatchEvent(new CustomEvent('viewchange', { detail: n }));
    };
    addEventListener('hashchange', show);
    show();
  }

  /* ---------- Hero carousel ---------- */
  function initHero() {
    const hero = $('#hero');
    if (!hero) return;
    const slides = $$('.hero-slide', hero), dots = $$('.hero-dot', hero);
    if (slides.length < 2) return;
    let i = 0, timer;
    const go = (n) => {
      i = (n + slides.length) % slides.length;
      slides.forEach((s, k) => s.classList.toggle('is-active', k === i));
      dots.forEach((d, k) => d.classList.toggle('on', k === i));
    };
    const stop = () => clearInterval(timer);
    const play = () => { stop(); if (!matchMedia('(prefers-reduced-motion: reduce)').matches) timer = setInterval(() => go(i + 1), 7000); };
    $('.prev', hero).onclick = () => { go(i - 1); play(); };
    $('.next', hero).onclick = () => { go(i + 1); play(); };
    dots.forEach((d, k) => { d.onclick = () => { go(k); play(); }; });
    hero.addEventListener('mouseenter', stop); hero.addEventListener('mouseleave', play);
    hero.addEventListener('focusin', stop); hero.addEventListener('focusout', play);
    play();
  }

  /* ---------- Countries ---------- */
  let COUNTRIES = { IN: 'India', US: 'United States', GB: 'United Kingdom', CA: 'Canada', AU: 'Australia', DE: 'Germany', FR: 'France', JP: 'Japan', BR: 'Brazil', AE: 'UAE', SG: 'Singapore' };
  let country = 'IN';
  try { country = localStorage.getItem('cr-country') || 'IN'; } catch (e) { /* ignore */ }
  const countryOptions = () => Object.entries(COUNTRIES).map(([c, n]) => `<option value="${c}"${c === country ? ' selected' : ''}>${esc(n)}</option>`).join('');
  const paintCountrySelects = () => $$('.js-country').forEach((s) => { s.innerHTML = countryOptions(); });
  document.addEventListener('change', (e) => {
    if (!e.target.classList.contains('js-country')) return;
    country = e.target.value;
    try { localStorage.setItem('cr-country', country); } catch (err) { /* ignore */ }
    paintCountrySelects();
    if ($('#movieModal').classList.contains('open') && current) loadWhere(current, openToken);
    if ($('#discoverGrid') && discoverStarted) { loadProviders(); loadDiscover(true); }
  });

  /* ---------- Movie dialog ---------- */
  const modal = $('#movieModal');
  const dialog = modal && $('.dialog', modal);
  const modalBody = $('#movieModalBody');
  let current = null, openToken = 0, lastFocus = null, dirty = false;

  const starsHtml = (n) => `<span class="stars">${[1, 2, 3, 4, 5].map((i) => `<i class="fas fa-star${i <= n ? ' on' : ''}"></i>`).join('')}</span>`;
  const posterCard = (m, meta) => `
    <div class="m-card"><button type="button" class="poster" data-ref="${esc(m.id)}" aria-label="Open details for ${esc(m.title)}">
      <img src="${esc(m.poster || NO_POSTER)}" alt="" loading="lazy">${m.rating && m.rating !== 'N/A' ? `<span class="poster-badge"><i class="fas fa-star"></i> ${esc(m.rating)}</span>` : ''}</button>
      <div class="m-card-body"><h4 title="${esc(m.title)}">${esc(m.title)}</h4><span class="m-meta">${esc(meta ?? m.year ?? '')}</span></div></div>`;
  const skeleton = `<div class="md-body"><div class="skel skel-block"></div><div class="skel skel-line" style="width:60%"></div><div class="skel skel-line" style="width:80%"></div><div class="skel skel-block"></div></div>`;

  async function openMovie(ref) {
    if (!modal) return;
    if (!modal.classList.contains('open')) lastFocus = document.activeElement;
    modal.classList.add('open'); modal.setAttribute('aria-hidden', 'false'); document.body.classList.add('no-scroll');
    modalBody.innerHTML = skeleton;
    modal.scrollTop = 0;
    dialog.focus();
    const token = ++openToken;
    try {
      const m = await api(`/api/movie/${encodeURIComponent(ref)}`);
      if (token !== openToken) return;
      current = m;
      renderMovie(m);
      loadWhere(m, token); loadReviews(m, token); loadSimilar(m, token);
    } catch (err) {
      if (token !== openToken) return;
      modalBody.innerHTML = `<div class="md-error"><p><i class="fas fa-triangle-exclamation"></i> ${esc(err.message || 'Could not load this movie.')}</p><p class="fine">Check your connection and try again.</p></div>`;
    }
  }

  function closeMovie() {
    if (!modal.classList.contains('open')) return;
    openToken++; current = null;
    modal.classList.remove('open'); modal.setAttribute('aria-hidden', 'true'); document.body.classList.remove('no-scroll');
    if (lastFocus && lastFocus.focus) lastFocus.focus();
    if (dirty) { dirty = false; refreshPartials(); }
  }

  function scoreBadges(m) {
    const out = [];
    if (m.imdb_rating) out.push(`<span class="score"><i class="fas fa-star" style="color:var(--warn)"></i> ${esc(m.imdb_rating)}<small>IMDb</small></span>`);
    if (m.rt_score) out.push(`<span class="score">${esc(m.rt_score)}<small>Rotten Tomatoes</small></span>`);
    if (!m.imdb_rating && m.tmdb_rating) out.push(`<span class="score"><i class="fas fa-star" style="color:var(--warn)"></i> ${esc(m.tmdb_rating)}<small>TMDB</small></span>`);
    if (m.community && m.community.votes) out.push(`<span class="score">${esc(m.community.average_stars)}/5<small>Community (${esc(m.community.votes)})</small></span>`);
    return out.join('');
  }

  function renderMovie(m) {
    const meta = [m.runtime ? `${m.runtime} min` : '', ...(m.genres || [])].filter(Boolean);
    modalBody.innerHTML = `
      <header class="md-head" style="--bd:url('${cssUrl(m.backdrop || m.poster)}')">
        <img class="md-poster" src="${esc(m.poster)}" alt="${esc(m.title)} poster">
        <div class="md-info">
          <h2 id="md-title">${esc(m.title)} <span class="md-year">(${esc(m.year)})</span></h2>
          ${m.tagline ? `<p class="md-tagline">${esc(m.tagline)}</p>` : ''}
          <div class="md-meta">${meta.map((x) => `<span class="tag">${esc(x)}</span>`).join('')}</div>
          <div class="md-scores">${scoreBadges(m)}</div>
          <p class="md-plot">${esc(m.plot)}</p>
          ${m.director ? `<p class="md-line"><b>Director</b>${esc(m.director)}</p>` : ''}
          ${(m.cast || []).length ? `<p class="md-line"><b>Cast</b>${m.cast.slice(0, 6).map((c) => esc(c.name)).join(', ')}</p>` : ''}
          <div class="md-actions">
            <button type="button" class="btn ${m.in_watchlist ? 'btn-ghost-light' : 'btn-primary'}" id="mdWatch"><i class="fas ${m.in_watchlist ? 'fa-check' : 'fa-plus'}"></i> ${m.in_watchlist ? 'In watchlist' : 'Watchlist'}</button>
            ${m.trailer_url ? `<a class="btn btn-ghost-light" href="${esc(m.trailer_url)}" target="_blank" rel="noopener noreferrer"><i class="fas fa-play"></i> Trailer</a>` : ''}
          </div>
        </div>
      </header>
      <div class="md-body">
        <section class="md-sec" id="mdWhere"><h3>Where to watch <select class="select js-country" aria-label="Country">${countryOptions()}</select></h3><div class="skel skel-block" style="height:70px"></div></section>
        <section class="md-sec" id="mdRate"><h3>${m.my_rating ? 'Your rating' : 'Rate this movie'}</h3>
          <div class="rate-box">
            <div class="star-input" id="mdStars" role="radiogroup" aria-label="Your rating">${[1, 2, 3, 4, 5].map((i) => `<button type="button" data-v="${i}" role="radio" aria-label="${i} star${i > 1 ? 's' : ''}"><i class="fas fa-star"></i></button>`).join('')}</div>
            <textarea class="input" id="mdReview" maxlength="2000" placeholder="Write a short review (optional)">${esc(m.my_rating ? m.my_rating.review || '' : '')}</textarea>
            <div><button type="button" class="btn btn-primary btn-sm" id="mdSave">Save rating</button></div>
          </div></section>
        <section class="md-sec" id="mdReviews"><h3>Community reviews</h3><div class="skel skel-block" style="height:60px"></div></section>
        <section class="md-sec" id="mdSimilar" hidden><h3>You might also like</h3><div class="row" id="mdSimilarRow"></div></section>
      </div>`;
    wireDialog(m);
  }

  function wireDialog(m) {
    let value = m.my_rating ? Math.round(m.my_rating.stars) : 0;
    const paint = (n) => $$('#mdStars button').forEach((b) => { b.classList.toggle('on', +b.dataset.v <= n); b.setAttribute('aria-checked', String(+b.dataset.v === value)); });
    paint(value);
    $$('#mdStars button').forEach((b) => {
      b.addEventListener('click', () => { value = +b.dataset.v; paint(value); });
      b.addEventListener('mouseenter', () => paint(+b.dataset.v));
      b.addEventListener('mouseleave', () => paint(value));
    });
    $('#mdSave').onclick = async (e) => {
      if (!value) return toast('Pick a star rating first', 'error');
      e.target.disabled = true;
      try {
        await api('/rate_movie', { method: 'POST', body: { movie_id: m.id, rating: value * 2, review: $('#mdReview').value.trim() } });
        toast('Rating saved', 'success'); dirty = true;
        loadReviews(m, openToken);
      } catch (err) { toast(err.message, 'error'); } finally { e.target.disabled = false; }
    };
    $('#mdWatch').onclick = async (e) => {
      const btn = e.currentTarget, adding = !m.in_watchlist;
      btn.disabled = true;
      try {
        await api(adding ? '/add_to_watchlist' : '/remove_from_watchlist', { method: 'POST', body: { movie_id: m.id } });
        m.in_watchlist = adding; dirty = true;
        btn.className = `btn ${adding ? 'btn-ghost-light' : 'btn-primary'}`;
        btn.innerHTML = `<i class="fas ${adding ? 'fa-check' : 'fa-plus'}"></i> ${adding ? 'In watchlist' : 'Watchlist'}`;
        toast(adding ? 'Added to watchlist' : 'Removed from watchlist', 'success');
      } catch (err) { toast(err.message, 'error'); } finally { btn.disabled = false; }
    };
  }

  const safeUrl = (u) => (/^https:\/\//.test(u || '') ? u : null);
  const anyLink = (w) => ['free', 'ads', 'flatrate', 'rent', 'buy'].some((k) => (w[k] || []).some((p) => safeUrl(p.url)));
  function provChip(p) {
    const inner = `${p.logo ? `<img src="${esc(p.logo)}" alt="" loading="lazy">` : ''}${esc(p.name)}`;
    const url = safeUrl(p.url);
    return url
      ? `<a class="prov" href="${esc(url)}" target="_blank" rel="noopener noreferrer" title="Open ${esc(p.name)}">${inner}<i class="fas fa-arrow-up-right-from-square ext" aria-hidden="true"></i></a>`
      : `<span class="prov">${inner}</span>`;
  }

  async function loadWhere(m, token) {
    const box = $('#mdWhere');
    if (!box) return;
    const head = `<h3>Where to watch <select class="select js-country" aria-label="Country">${countryOptions()}</select></h3>`;
    try {
      const w = await api(`/api/movie/${encodeURIComponent(m.id)}/where-to-watch?country=${country}`);
      if (token !== openToken) return;
      const groups = [['free', 'Free'], ['ads', 'Free with ads'], ['flatrate', 'Subscription'], ['rent', 'Rent'], ['buy', 'Buy']];
      const rows = groups.filter(([k]) => (w[k] || []).length).map(([k, label]) => `
        <div class="prov-group"><span class="prov-label">${label}</span><div class="prov-list">${w[k].map((p) => provChip(p)).join('')}</div></div>`).join('');
      const icon = { free: 'fa-gift', subscription: 'fa-tv', rent_buy: 'fa-ticket', unavailable: 'fa-circle-info' }[w.summary.kind] || 'fa-tv';
      const link = /^https:\/\//.test(w.link || '') ? `<a href="${esc(w.link)}" target="_blank" rel="noopener noreferrer">Full list</a> · ` : '';
      box.innerHTML = `${head}<div class="wtw-summary ${esc(w.summary.kind)}"><i class="fas ${icon}"></i><span>${esc(w.summary.text)}</span></div>${rows}<p class="fine">${anyLink(w) ? 'Tap a platform to open its site. ' : ''}${esc(w.plan_note)}</p><p class="fine">${link}${esc(w.attribution)}</p>`;
    } catch (err) {
      if (token !== openToken) return;
      box.innerHTML = `${head}<p class="fine">Availability is unavailable right now.</p>`;
    }
  }

  async function loadReviews(m, token) {
    const box = $('#mdReviews');
    if (!box) return;
    try {
      const d = await api(`/api/movie/${encodeURIComponent(m.id)}/reviews`);
      if (token !== openToken) return;
      const me = document.body.dataset.user;
      const s = d.sentiment;
      const sent = s ? `<div class="sent-bar" aria-hidden="true"><i class="p" style="width:${s.counts.positive * 100 / s.reviews}%"></i><i class="m" style="width:${s.counts.mixed * 100 / s.reviews}%"></i><i class="n" style="width:${s.counts.negative * 100 / s.reviews}%"></i></div>
        <div class="sent-legend"><span>${s.counts.positive} positive</span><span>${s.counts.mixed} mixed</span><span>${s.counts.negative} negative</span></div>` : '';
      box.innerHTML = `<h3>Community reviews ${s ? `<span class="badge ${esc(s.overall)}">${esc(s.overall)} overall</span>` : ''}</h3>${sent}` +
        (d.reviews.length ? d.reviews.map((r) => `
          <div class="review-item"><span class="avatar sm" aria-hidden="true">${esc(r.user.slice(0, 1))}</span>
            <div><div class="review-top"><b>${esc(r.user)}</b>${starsHtml(Math.round(r.stars))}${r.sentiment ? `<span class="badge ${esc(r.sentiment)}">${esc(r.sentiment)}</span>` : ''}</div>
              <p class="review-text">${esc(r.review)}</p>
              ${r.user !== me ? `<button type="button" class="like-btn${r.liked_by_me ? ' liked' : ''}" data-rating-id="${r.id}" aria-label="Like this review"><i class="fas fa-heart"></i><span class="like-count">${r.likes}</span></button>` : ''}</div></div>`).join('')
          : '<p class="fine">No written reviews yet. Be the first.</p>');
    } catch (err) { if (token === openToken) box.innerHTML = '<h3>Community reviews</h3><p class="fine">Could not load reviews.</p>'; }
  }

  async function loadSimilar(m, token) {
    try {
      const list = await api(`/api/movie/${encodeURIComponent(m.id)}/similar`);
      if (token !== openToken || !list.length) return;
      $('#mdSimilarRow').innerHTML = list.slice(0, 10).map((x) => posterCard(x)).join('');
      $('#mdSimilar').hidden = false;
    } catch (e) { /* section stays hidden */ }
  }

  /* Re-fetch the page and swap the data-refresh blocks (ratings, watchlist, stats, for-you). */
  async function refreshPartials() {
    if (!$('[data-refresh]')) return;
    try {
      const html = await (await fetch(location.pathname, { credentials: 'same-origin' })).text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      $$('[data-refresh]').forEach((el) => {
        const fresh = doc.querySelector(`[data-refresh="${el.dataset.refresh}"]`);
        if (fresh) el.innerHTML = fresh.innerHTML;
      });
    } catch (e) { /* the next navigation will pick the change up */ }
  }

  /* ---------- Global click handling ---------- */
  document.addEventListener('submit', (e) => {              // <form data-confirm="..."> asks before submitting
    const msg = e.target.dataset && e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });
  document.addEventListener('click', async (e) => {
    const t = e.target;
    const dismiss = t.closest('.toast button');              // server-rendered flash messages
    if (dismiss) { dismiss.parentElement.remove(); return; }
    const open = t.closest('[data-ref]');
    if (open) { e.preventDefault(); closeResults(); openMovie(open.dataset.ref); return; }
    const rm = t.closest('[data-remove-watch]');
    if (rm) {
      try { await api('/remove_from_watchlist', { method: 'POST', body: { movie_id: rm.dataset.removeWatch } }); toast('Removed from watchlist', 'success'); refreshPartials(); }
      catch (err) { toast(err.message, 'error'); }
      return;
    }
    const like = t.closest('.like-btn');
    if (like) {
      try {
        const d = await api(`/like_review/${like.dataset.ratingId}`, { method: 'POST' });
        $$(`.like-btn[data-rating-id="${like.dataset.ratingId}"]`).forEach((b) => { b.classList.toggle('liked', d.liked); const c = $('.like-count', b); if (c) c.textContent = d.count; });
      } catch (err) { toast(err.message, 'error'); }
      return;
    }
    if (t === modal || t.closest('#dialogClose')) closeMovie();
    if (!t.closest('.search')) closeResults();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeMovie(); closeResults(); closeNav(); }
    if (e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) && $('#globalSearch')) { e.preventDefault(); $('#globalSearch').focus(); }
  });

  /* ---------- Search ---------- */
  const input = $('#globalSearch'), results = $('#searchResults');
  let reqId = 0, kb = -1;
  function closeResults() { if (results) { results.classList.remove('open'); kb = -1; } }
  async function liveSearch() {
    const q = input.value.trim();
    if (q.length < 2) return closeResults();
    const id = ++reqId;
    try {
      const list = await api(`/api/search?q=${encodeURIComponent(q)}`);
      if (id !== reqId) return; // a newer keystroke already replaced this request
      results.innerHTML = list.length ? list.map((m) => `
        <button type="button" class="sr-item" role="option" data-ref="${esc(m.ref)}"><img src="${esc(m.poster || NO_POSTER)}" alt="" loading="lazy">
          <span><span class="sr-title">${esc(m.title)}</span><br><span class="sr-sub">${esc(m.year)}${m.genres && m.genres.length ? ' · ' + esc(m.genres.slice(0, 2).join(', ')) : ''}</span></span>
          ${m.rating && m.rating !== 'N/A' ? `<span class="sr-sub"><i class="fas fa-star" style="color:var(--warn)"></i> ${esc(m.rating)}</span>` : ''}</button>`).join('')
        : '<div class="sr-empty">No movies found</div>';
      results.classList.add('open'); kb = -1;
    } catch (err) { results.innerHTML = '<div class="sr-empty">Search is unavailable right now</div>'; results.classList.add('open'); }
  }
  if (input) {
    input.addEventListener('input', debounce(liveSearch, 250));
    input.addEventListener('focus', () => { if (results.children.length) results.classList.add('open'); });
    input.addEventListener('keydown', (e) => {
      const items = $$('.sr-item', results);
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault(); if (!items.length) return;
        kb = (kb + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        items.forEach((it, i) => it.classList.toggle('kb', i === kb));
        items[kb].scrollIntoView({ block: 'nearest' });
      } else if (e.key === 'Enter') { e.preventDefault(); (items[kb] || items[0])?.click(); }
    });
  }

  /* ---------- Discover ---------- */
  let discoverStarted = false;
  const D = { genre: '', provider: '', free: false, page: 1, total: 1, busy: false };
  async function loadProviders() {
    const box = $('#providerChips');
    if (!box) return;
    try {
      const list = await api(`/api/providers?country=${country}`);
      box.innerHTML = `<button type="button" class="chip${D.provider ? '' : ' is-on'}" data-provider="">Any</button>` +
        list.map((p) => `<button type="button" class="chip${String(p.id) === D.provider ? ' is-on' : ''}" data-provider="${p.id}">${p.logo ? `<img src="${esc(p.logo)}" alt="">` : ''}${esc(p.name)}</button>`).join('');
    } catch (e) { box.innerHTML = '<span class="m-meta">Platforms unavailable</span>'; }
  }
  async function loadDiscover(reset) {
    if (D.busy) return;
    D.busy = true;
    if (reset) { D.page = 1; $('#discoverGrid').innerHTML = Array.from({ length: 12 }, () => '<div class="skel" style="aspect-ratio:2/3"></div>').join(''); }
    const qs = new URLSearchParams({ sort: $('#sortSelect').value, page: D.page, country });
    if (D.genre) qs.set('genre', D.genre);
    if ($('#yearSelect').value) qs.set('year', $('#yearSelect').value);
    if (D.provider) qs.set('provider', D.provider);
    if (D.free) qs.set('free', '1');
    try {
      const d = await api(`/api/discover?${qs}`);
      D.total = d.total_pages;
      const html = d.results.map((m) => posterCard(m)).join('');
      $('#discoverGrid').innerHTML = reset ? html : $('#discoverGrid').innerHTML + html;
      $('#discoverEmpty').hidden = !!$('#discoverGrid').children.length;
      $('#discoverMore').hidden = D.page >= D.total;
    } catch (err) { toast(err.message, 'error'); if (reset) $('#discoverGrid').innerHTML = ''; }
    D.busy = false;
  }
  function initDiscover() {
    if (!$('#discoverGrid')) return;
    document.addEventListener('viewchange', (e) => {
      if (e.detail === 'discover' && !discoverStarted) { discoverStarted = true; loadProviders(); loadDiscover(true); }
    });
    $('#genreChips').addEventListener('click', (e) => {
      const b = e.target.closest('[data-genre]'); if (!b) return;
      D.genre = b.dataset.genre; $$('#genreChips .chip').forEach((c) => c.classList.toggle('is-on', c === b)); loadDiscover(true);
    });
    $('#providerChips').addEventListener('click', (e) => {
      const b = e.target.closest('[data-provider]'); if (!b) return;
      D.provider = b.dataset.provider; $$('#providerChips .chip').forEach((c) => c.classList.toggle('is-on', c === b)); loadDiscover(true);
    });
    $('#freeToggle').onclick = (e) => { D.free = !D.free; e.currentTarget.classList.toggle('is-on', D.free); loadDiscover(true); };
    $('#sortSelect').onchange = () => loadDiscover(true);
    $('#yearSelect').onchange = () => loadDiscover(true);
    $('#discoverMore').onclick = () => { D.page += 1; loadDiscover(false); };
  }

  /* ---------- Watchlist optimizer ---------- */
  function initOptimizer() {
    const btn = $('#optimizeBtn');
    if (!btn) return;
    btn.onclick = async () => {
      const out = $('#optimizeResult');
      btn.disabled = true; out.innerHTML = '<div class="skel skel-block" style="height:84px"></div>';
      try {
        const r = await api(`/api/watchlist/where-to-watch?country=${country}`);
        const step = (icon, cls, title, body) => `<div class="opt-step"><span class="opt-ico ${cls}"><i class="fas ${icon}"></i></span><div><h4>${title}</h4><p>${body}</p></div></div>`;
        const steps = [];
        if (r.free.length) steps.push(step('fa-gift', 'free', 'Free to watch', r.free.map(esc).join(', ')));
        r.plan.forEach((p, i) => steps.push(step('fa-tv', '', `${i === 0 ? 'Start with' : 'Then add'} ${safeUrl(p.url) ? `<a href="${esc(safeUrl(p.url))}" target="_blank" rel="noopener noreferrer">${esc(p.provider)}</a>` : esc(p.provider)}`, `Covers ${p.titles.length}: ${p.titles.map(esc).join(', ')}`)));
        if (r.rent_buy.length) steps.push(step('fa-ticket', '', 'Rent or buy', r.rent_buy.map((m) => `${esc(m.title)}${m.providers.length ? ` (${m.providers.map(esc).join(', ')})` : ''}`).join('; ')));
        if (r.unavailable.length) steps.push(step('fa-circle-info', '', 'Not available in this country', r.unavailable.map(esc).join(', ')));
        if (!steps.length) steps.push('<p class="empty">Add movies to your watchlist first.</p>');
        out.innerHTML = steps.join('') + `<p class="fine">${esc(r.plan_note)}</p><p class="fine">${esc(r.attribution)}</p>`;
      } catch (err) { out.innerHTML = ''; toast(err.message, 'error'); }
      btn.disabled = false;
    };
  }

  async function loadCountries() {
    if (!loggedIn) return;
    try {
      const list = await api('/api/countries');
      if (!list.length) return;
      COUNTRIES = Object.fromEntries(list.map((c) => [c.code, c.name]));
      if (!COUNTRIES[country]) country = 'IN';
      paintCountrySelects();
    } catch (e) { /* keep the short built-in list */ }
  }

  /* ---------- Delete account (profile page) ---------- */
  function initDelete() {
    const dlg = $('#deleteDialog');
    if (!dlg) return;
    const input = $('#confirmUsername', dlg), go = $('#confirmDelete', dlg);
    $('#openDelete').onclick = () => { input.value = ''; go.disabled = true; dlg.showModal(); input.focus(); };
    $('#cancelDelete').onclick = () => dlg.close();
    input.addEventListener('input', () => { go.disabled = input.value !== dlg.dataset.username; });
    dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
  }

  paintCountrySelects();
  loadCountries(); initDelete();
  initViews(); initHero(); initDiscover(); initOptimizer();
})();
