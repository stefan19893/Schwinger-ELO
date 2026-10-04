/* Schwinger-ELO: shared helpers (namespace SE). No dependencies.
 * Every string that comes from the data goes through SE.esc before it is put into HTML. */
(function () {
  'use strict';

  var SE = window.SE = {};

  SE.REPO = 'https://github.com/stefan19893/Schwinger-ELO';
  SE.ID_RE = /^[a-z0-9-]{1,120}$/;

  SE.TV = {
    BKSV: 'Bernisch-Kantonaler Schwingerverband',
    ISV: 'Innerschweizer Schwingerverband',
    NOSV: 'Nordostschweizer Schwingerverband',
    NWSV: 'Nordwestschweizer Schwingerverband',
    SWSV: 'Südwestschweizer Schwingerverband'
  };

  SE.CATEGORY = {
    ESAF: 'Eidgenössisch',
    Bergkranz: 'Bergkranzfest',
    Teilverband: 'Teilverbandsfest',
    Kantonal: 'Kantonalfest',
    Gauverband: 'Gauverbandsfest',
    Regional: 'Regionalfest'
  };

  SE.EIDG = {
    ESAF: 'Eidgenössisches (ESAF)',
    Kilchberg: 'Kilchberger Schwinget',
    Unspunnen: 'Unspunnen-Schwinget',
    Jubilaeum: 'ESV-Jubiläumsfest'
  };

  // athletes.json / alltime flags
  SE.F_RANKED = 1; SE.F_FEW = 2; SE.F_INACTIVE = 4; SE.F_UNCERTAIN = 8;

  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  SE.esc = function (v) {
    if (v === null || v === undefined) { return ''; }
    return String(v).replace(/[&<>"']/g, function (c) { return ESC[c]; });
  };

  SE.$ = function (id) { return document.getElementById(id); };

  SE.param = function (name) {
    return new URLSearchParams(window.location.search).get(name);
  };

  SE.getJSON = function (path) {
    return fetch(path, { credentials: 'same-origin' }).then(function (res) {
      if (!res.ok) {
        var err = new Error('HTTP ' + res.status + ' ' + path);
        err.status = res.status;
        throw err;
      }
      return res.json();
    });
  };

  /* {cols, rows} -> array of objects */
  SE.table = function (t) {
    var cols = t.cols, out = new Array(t.rows.length);
    for (var i = 0; i < t.rows.length; i++) {
      var o = {}, r = t.rows[i];
      for (var j = 0; j < cols.length; j++) { o[cols[j]] = r[j]; }
      out[i] = o;
    }
    return out;
  };

  // ------------------------------------------------------------------ formatting
  SE.num = function (n, digits) {
    if (n === null || n === undefined || isNaN(n)) { return '–'; }
    return Number(n).toLocaleString('de-CH', {
      minimumFractionDigits: digits || 0, maximumFractionDigits: digits || 0
    });
  };

  /* rating without thousands separator: 2703 */
  SE.rating = function (n) {
    return (n === null || n === undefined) ? '–' : String(Math.round(n));
  };

  SE.signed = function (n) {
    if (n === null || n === undefined) { return ''; }
    var r = Math.round(n);
    return (r > 0 ? '+' : r < 0 ? '−' : '±') + Math.abs(r);
  };

  /* a change with one decimal: +31.2, −4.0, ±0.0 (contributions per Gang) */
  SE.signed1 = function (n) {
    if (n === null || n === undefined) { return ''; }
    var r = Math.round(n * 10) / 10;
    return (r > 0 ? '+' : r < 0 ? '−' : '±') + Math.abs(r).toFixed(1);
  };

  SE.date = function (iso) {
    if (!iso) { return '–'; }
    return iso.slice(8, 10) + '.' + iso.slice(5, 7) + '.' + iso.slice(0, 4);
  };

  SE.monthYear = function (iso) {
    return iso ? iso.slice(5, 7) + '.' + iso.slice(0, 4) : '–';
  };

  SE.grade = function (g) {
    return (g === null || g === undefined) ? '–' : Number(g).toFixed(2);
  };

  SE.category = function (cat, eidg) {
    if (cat === 'ESAF' && eidg && SE.EIDG[eidg]) { return SE.EIDG[eidg]; }
    return SE.CATEGORY[cat] || cat || '';
  };

  SE.athleteUrl = function (id) { return 'athlete.html?id=' + encodeURIComponent(id); };

  SE.festUrl = function (id, athleteId) {
    return 'fests.html?id=' + encodeURIComponent(id) +
      (athleteId ? '&a=' + encodeURIComponent(athleteId) : '');
  };

  /* "Klub · Jg. 1998 · NOSV" - what tells namesakes apart */
  SE.subline = function (o, withYears) {
    var parts = [];
    if (o.club) { parts.push(SE.esc(o.club)); }
    if (o.by) { parts.push('Jg. ' + SE.esc(o.by)); }
    if (o.tv) { parts.push('<abbr title="' + SE.esc(SE.TV[o.tv] || o.tv) + '">' + SE.esc(o.tv) + '</abbr>'); }
    if (withYears && o.first) {
      /* nf: the number of festivals, given where nothing else tells two namesakes apart */
      parts.push((o.nf ? SE.num(o.nf) + (o.nf === 1 ? ' Fest ' : ' Feste ') : 'Feste ') +
        (o.first === o.last ? SE.esc(o.first) : SE.esc(o.first) + '–' + SE.esc(o.last)));
    }
    return parts.join(' · ');
  };

  /* Shown with an athlete whose search row equals a namesake's in every field (old
   * sheets: a name and little else). The page does not pretend to know more. */
  SE.TWIN_NOTE = 'Gleichnamiger Schwinger mit denselben Angaben – nur an der Zahl der Feste zu unterscheiden';

  /* What ratings of different years can be compared: one wording for the start page, the
   * comparison and the about page. The years are findings of the model evaluation on the
   * history from 2004 (docs/SPEC.md §4.2), the first season comes from meta.json. */
  SE.SCALE_FROM = 2016;
  SE.scaleText = function (meta) {
    var first = meta && typeof meta.first_season === 'number' ? meta.first_season : null;
    var s = 'Wertungen ab etwa ' + SE.SCALE_FROM + ' sind untereinander vergleichbar. Davor liegen sie systematisch tiefer';
    if (first !== null && first <= 2006) {
      return s + ': ' + first + '–2007 baut sich die Skala erst auf (alle beginnen ' + first + ' bei 1500); ' +
        '2008–2011 sind nur Kranzfeste erfasst, und die Spitze liegt rund 150 Punkte tiefer als ab ' + SE.SCALE_FROM + '; ' +
        '2012–2015 wächst die Skala mit den Regionalfesten auf den heutigen Stand.';
    }
    return s + ', weil sich die Skala nach dem Start bei 1500 erst aufbaut.';
  };

  SE.uncertainMark = function (unc) {
    if (!unc) { return ''; }
    return ' <abbr class="se-badge se-badge-warn" title="Identität unsicher: Namensvetter liessen sich in den Ranglisten nicht sicher trennen">?</abbr>';
  };

  /* No id = no profile: an athlete without rated bouts is shown by name ("ohne Wertung"),
   * a row whose name could not be read has no name either, and an athlete who is not
   * certainly of the publication age (anon) is listed without name and without rating. */
  SE.athleteLink = function (o) {
    if (o.anon) {
      return '<span class="text-stone-500 dark:text-stone-400" title="Wer nicht sicher volljährig ist, wird nicht mit Namen ' +
        'aufgeführt, und für ihn wird keine Wertung angezeigt; die Gänge zählen trotzdem">Jungschwinger, Name nicht veröffentlicht</span>';
    }
    if (!o.id && o.name) {
      return '<span class="font-medium">' + SE.esc(o.name) + '</span> <span class="se-badge se-badge-muted" ' +
        'title="Nur an Festen angetreten, die nicht für die Wertung zählen; deshalb ohne Wertung und ohne Profil">ohne Wertung</span>';
    }
    if (!o.id) { return '<span class="text-stone-500 dark:text-stone-400">Name nicht lesbar</span>'; }
    return '<a class="se-link font-medium" href="' + SE.athleteUrl(o.id) + '">' + SE.esc(o.name) + '</a>' +
      SE.uncertainMark(o.unc);
  };

  SE.note = function (html, tone) {
    return '<div class="se-note' + (tone === 'info' ? ' se-note-info' : '') + '">' + html + '</div>';
  };

  SE.showError = function (el, err) {
    el.innerHTML = SE.note('Die Daten konnten nicht geladen werden. Bitte Seite neu laden. ' +
      '<span class="block text-xs opacity-70">' + SE.esc(err && err.message ? err.message : err) + '</span>');
  };

  // ------------------------------------------------------------------ search
  SE.norm = function (s) {
    return String(s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '')
      .replace(/ß/g, 'ss').replace(/ae|oe|ue/g, function (m) { return m.charAt(0); })
      .replace(/[^a-z0-9]+/g, ' ').trim();
  };

  var searchIndex = null, searchPromise = null;

  SE.loadSearchIndex = function () {
    if (!searchPromise) {
      searchPromise = SE.getJSON('data/athletes.json').then(function (t) {
        searchIndex = SE.table(t);
        var twins = t.twins || {};
        for (var i = 0; i < searchIndex.length; i++) {
          var a = searchIndex[i];
          if (twins[a.id]) { a.nf = twins[a.id][0]; a.twin = 1; }
          a.key = (SE.norm(a.name) + ' ' + SE.norm(a.club)).split(' ');
        }
        return searchIndex;
      });
      searchPromise.catch(function () { searchPromise = null; });
    }
    return searchPromise;
  };

  /* All query tokens must be the beginning of a name or club word. Best first:
   * ranked athletes by rank, then by peak / rating. */
  SE.search = function (index, query, limit) {
    var tokens = SE.norm(query).split(' ').filter(Boolean);
    if (!tokens.length) { return { hits: [], total: 0 }; }
    var hits = [];
    for (var i = 0; i < index.length; i++) {
      var key = index[i].key, ok = true;
      for (var t = 0; t < tokens.length && ok; t++) {
        var found = false;
        for (var k = 0; k < key.length; k++) {
          if (key[k].lastIndexOf(tokens[t], 0) === 0) { found = true; break; }
        }
        ok = found;
      }
      if (ok) { hits.push(index[i]); }
    }
    hits.sort(function (a, b) {
      var ra = a.rank === null ? 1e9 : a.rank, rb = b.rank === null ? 1e9 : b.rank;
      if (ra !== rb) { return ra - rb; }
      var pa = a.peak || a.rating || 0, pb = b.peak || b.rating || 0;
      if (pa !== pb) { return pb - pa; }
      return a.name < b.name ? -1 : a.name > b.name ? 1 : (a.id < b.id ? -1 : 1);
    });
    return { hits: hits.slice(0, limit || 25), total: hits.length };
  };

  SE.searchStatus = function (a) {
    if (a.rank !== null) { return 'Rang ' + SE.num(a.rank) + ' · ' + SE.rating(a.rating); }
    if (a.flags & SE.F_INACTIVE) {
      return 'nicht mehr aktiv · ' + (a.peak ? 'Bestwert ' + SE.rating(a.peak) : 'Wertung ' + SE.rating(a.rating));
    }
    return 'provisorisch · ' + SE.rating(a.rating);
  };

  SE.searchResultsHtml = function (res) {
    if (!res.total) {
      return '<p class="p-3 text-sm text-stone-500 dark:text-stone-400">Kein Schwinger gefunden.</p>';
    }
    var html = '<ul>';
    res.hits.forEach(function (a) {
      html += '<li><a class="se-hit" href="' + SE.athleteUrl(a.id) + '">' +
        '<span class="block font-medium">' + SE.esc(a.name) +
        ((a.flags & SE.F_UNCERTAIN) ? SE.uncertainMark(1) : '') + '</span>' +
        '<span class="block text-xs text-stone-500 dark:text-stone-400">' + SE.subline(a, true) + '</span>' +
        '<span class="block text-xs text-stone-500 dark:text-stone-400">' + SE.esc(SE.searchStatus(a)) + '</span>' +
        (a.twin ? '<span class="block text-xs text-stone-500 dark:text-stone-400">' + SE.TWIN_NOTE + '</span>' : '') +
        '</a></li>';
    });
    html += '</ul>';
    if (res.total > res.hits.length) {
      html += '<p class="px-3 py-2 text-xs text-stone-500 dark:text-stone-400">' +
        SE.num(res.total - res.hits.length) + ' weitere Treffer – Suche verfeinern (z.B. Klub ergänzen).</p>';
    }
    return html;
  };

  function initSearch(root) {
    var input = root.querySelector('input');
    var panel = root.querySelector('[data-search-results]');
    if (!input || !panel) { return; }
    var seq = 0;

    function close() { panel.classList.add('hidden'); }

    function run() {
      var q = input.value, mine = ++seq;
      if (!SE.norm(q)) { close(); return; }
      panel.classList.remove('hidden');
      if (!searchIndex) {
        panel.innerHTML = '<p class="p-3 text-sm text-stone-500 dark:text-stone-400">Suche wird geladen …</p>';
      }
      SE.loadSearchIndex().then(function (index) {
        if (mine !== seq) { return; }
        panel.innerHTML = SE.searchResultsHtml(SE.search(index, q, 25));
      }).catch(function (err) {
        if (mine === seq) { SE.showError(panel, err); }
      });
    }

    input.addEventListener('focus', function () { SE.loadSearchIndex().catch(function () {}); run(); });
    input.addEventListener('input', run);
    input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') { close(); input.blur(); }
      if (ev.key === 'Enter') {
        var first = panel.querySelector('a');
        if (first && !panel.classList.contains('hidden')) { window.location.href = first.href; }
      }
    });
    document.addEventListener('click', function (ev) {
      if (!root.contains(ev.target)) { close(); }
    });
  }

  // ------------------------------------------------------------------ page frame
  var metaPromise = null;
  SE.meta = function () {
    if (!metaPromise) { metaPromise = SE.getJSON('data/meta.json'); }
    return metaPromise;
  };

  function initFrame() {
    var roots = document.querySelectorAll('[data-search]');
    for (var i = 0; i < roots.length; i++) { initSearch(roots[i]); }
    SE.meta().then(function (meta) {
      var el = SE.$('se-asof');
      if (el) { el.textContent = meta.as_of ? 'Datenstand: ' + SE.date(meta.as_of) : 'Noch keine Daten'; }
      var since = SE.$('se-since');
      if (since && meta.first_season) { since.textContent = ' seit ' + meta.first_season; }
      var banner = SE.$('se-banner');
      if (banner && meta.empty) {
        banner.innerHTML = SE.note('Es sind noch keine Daten vorhanden. Zuerst die Pipeline laufen lassen ' +
          '(<code>python -m src.cli all</code>).');
      } else if (banner && meta.sample) {
        banner.innerHTML = SE.note('Demo-Daten: nur ' + SE.num(meta.counts.festivals) +
          ' Feste. Die Zahlen sind nicht aussagekräftig.', 'info');
      }
    }).catch(function () { /* each page reports its own loading errors */ });
  }

  // Make script errors visible instead of leaving an empty page.
  window.addEventListener('error', function (ev) {
    var el = SE.$('se-banner');
    if (el && !el.getAttribute('data-error')) {
      el.setAttribute('data-error', '1');
      el.innerHTML = SE.note('Auf dieser Seite ist ein Fehler aufgetreten. ' +
        '<span class="block text-xs opacity-70">' + SE.esc(ev.message || '') + '</span>');
    }
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initFrame);
  } else {
    initFrame();
  }
})();
