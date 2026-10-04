/* Schwinger-ELO: comparison of athletes (compare.html?ids=<athlete_id>,<athlete_id>,...).
 *
 * Per selected athlete the page loads his history file (figures, chart, seasons, common
 * festivals) and, from two athletes on, his bouts file (direct bouts). Nothing else is
 * loaded up front; the search index only when the picker is used or a link is outdated.
 * The ids come from the URL and are untrusted: they are validated before they become
 * part of a file path, and everything shown goes through SE.esc.
 *
 * The career chart has four horizontal axes: calendar time (default), the number of
 * bouts (?x=gaenge), age (?x=alter) and the season of the career (?x=saison). The
 * parameter is compared with the known values and never shown. */
(function () {
  'use strict';
  var SE = window.SE;
  var MAX = 6;
  var PAGE = 25;
  var IDLE_WARN_DAYS = 180;
  var SCALE_SETTLED = '2016-01-01';
  var B_SCHLUSSGANG = 1, B_EXTRA = 2, B_GANG_UNCERTAIN = 8, B_UNRATED = 16;
  var FIRST_BOUTS = 60;        // the zoom button "Erste 60 Gänge"
  var DOT_PX = 3;              // single Gänge get a point when each has this many pixels

  var view = SE.$('se-view'), picked = SE.$('se-picked'), slots = SE.$('se-slots');
  var input = SE.$('se-add'), panel = SE.$('se-add-results'), hint = SE.$('se-add-hint');

  /* One entry per id of the URL, in that order.
   * status: loading | ok | missing (no such profile) | error (could not be loaded) */
  var entries = [];
  var dropped = { invalid: 0, over: 0 };
  var ui = { focus: null, common: 'all', commonShown: PAGE, zoom: 'all', x: 'time' };
  /* ?x= value per axis; time has none. Only these fixed values are ever accepted. */
  var X_PARAM = { bouts: 'gaenge', age: 'alter', season: 'saison' };
  var firstSeason = null;      // meta.first_season, for the caveats of the axes
  var metaState = 'pending';   // pending | ok | missing (meta.json failed or names no first season)
  var scaleNoted = false;      // the scale note of caveats() is on the page
  var EARLY_SEASONS = 2;       // the first seasons of the data are thin: a debut there may be none
  var ADULT_AGE = 20;          // first recorded in the year of this birthday or later: likely not a debut
  var chart = null;

  /* Colour-blind-safe set (after Okabe and Ito), one variant per colour scheme; every
   * athlete also gets his own point shape, so colour is never the only key. */
  var LIGHT = ['#0072b2', '#d55e00', '#007a5a', '#a8508a', '#a87800', '#1c1917'];
  var DARK = ['#56b4e9', '#f28e4e', '#2fcfa0', '#e29ac6', '#f0c245', '#f5f5f4'];
  var SYMBOLS = ['circle', 'rect', 'triangle', 'diamond', 'path://M0 0H12L6 11Z',
    'path://M4 0h4v4h4v4H8v4H4V8H0V4h4z'];
  var SHAPES = ['<circle cx="7" cy="7" r="5"/>', '<rect x="2.5" y="2.5" width="9" height="9"/>',
    '<path d="M7 1.5 13 12.5H1Z"/>', '<path d="M7 1 13 7 7 13 1 7Z"/>', '<path d="M1 1.5H13L7 12.5Z"/>',
    '<path d="M5 1h4v4h4v4H9v4H5V9H1V5h4z"/>'];

  function isDark() {
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  }

  function colour(e) { return (isDark() ? DARK : LIGHT)[e.slot]; }

  function swatch(e) {
    return '<svg class="shrink-0" width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" fill="' +
      colour(e) + '">' + SHAPES[e.slot] + '</svg>';
  }

  // ------------------------------------------------------------------ selection and URL
  /* "a,b,c" -> valid, distinct ids (at most MAX); counts what was thrown away. */
  function parseIds(raw) {
    var out = [], seen = {};
    dropped = { invalid: 0, over: 0 };
    String(raw || '').slice(0, 2000).split(',').forEach(function (part) {
      var id = part.trim();
      if (!id) { return; }
      if (!SE.ID_RE.test(id)) { dropped.invalid++; return; }
      if (seen[id]) { return; }
      seen[id] = true;
      if (out.length >= MAX) { dropped.over++; return; }
      out.push(id);
    });
    return out;
  }

  /* The axis of the chart: one of three known values, absent or anything else = time.
   * The raw value is only compared, never shown or stored. */
  function parseX(raw) {
    return raw === X_PARAM.bouts ? 'bouts' : raw === X_PARAM.age ? 'age' : raw === X_PARAM.season ? 'season' : 'time';
  }

  /* Time is the default and leaves the address as it always was. */
  function compareUrl(ids, x) {
    var mode = x || ui.x, v = mode === 'bouts' || mode === 'age' || mode === 'season' ? X_PARAM[mode] : '';
    return 'compare.html' + (ids.length ? '?ids=' + ids.map(encodeURIComponent).join(',') : '') +
      (v ? (ids.length ? '&' : '?') + 'x=' + v : '');
  }

  function ids() { return entries.map(function (e) { return e.id; }); }

  function ok() { return entries.filter(function (e) { return e.status === 'ok'; }); }

  function without(id) { return ids().filter(function (x) { return x !== id; }); }

  /* Keeps what is loaded and every athlete's colour; a new athlete takes the first
   * free colour, so removing one never recolours the others. */
  function setIds(list, push) {
    var old = {}, used = {};
    entries.forEach(function (e) { old[e.id] = e; });
    entries = list.map(function (id) { return old[id] || { id: id, status: 'loading', slot: -1 }; });
    entries.forEach(function (e) { if (e.slot >= 0) { used[e.slot] = true; } });
    entries.forEach(function (e) {
      if (e.slot >= 0) { return; }
      var s = 0;
      while (used[s]) { s++; }
      e.slot = s;
      used[s] = true;
    });
    if (ui.focus && list.indexOf(ui.focus) === -1) { ui.focus = null; }
    ui.commonShown = PAGE;
    ui.zoom = 'all';
    if (push && window.history && window.history.pushState) {
      window.history.pushState(null, '', compareUrl(list));
    }
    load();
  }

  function load() {
    entries.forEach(function (e) {
      if (e.status !== 'loading' || e.pending) { return; }
      e.pending = true;
      var id = e.id;   // validated by parseIds / add (SE.ID_RE) before it gets here
      SE.getJSON('data/history/history_' + id + '.json').then(function (h) {
        e.h = h;
        e.rows = SE.table(h.history);
        e.status = 'ok';
      }).catch(function (err) {
        e.status = err.status === 404 ? 'missing' : 'error';
        e.error = err;
      }).then(function () { e.pending = false; renderLoaded(); });
    });
    render();
  }

  /* The bouts files are needed for a comparison, i.e. from two athletes on, and for the
   * Gänge of the bout axis, there also for a single athlete. */
  function loadBouts() {
    var list = ok();
    if (list.length < 2 && ui.x !== 'bouts') { return; }
    list.forEach(function (e) {
      if (e.bouts || e.boutsPending || e.boutsError) { return; }
      e.boutsPending = true;
      var id = e.id;
      SE.getJSON('data/bouts/bouts_' + id + '.json').then(function (b) {
        e.bouts = b;
      }).catch(function (err) {
        e.boutsError = err;
      }).then(function () {
        e.boutsPending = false;
        /* the bout axis draws the Gänge from these files: once, when the last one is in */
        if (ui.x === 'bouts' && !ok().some(function (o) { return o.boutsPending; })) {
          renderLoaded();
        } else {
          renderDuels();
          renderCommon();
        }
      });
    });
  }

  // ------------------------------------------------------------------ small helpers
  function has(h, reason) { return h.provisional.indexOf(reason) !== -1; }

  function muted(html) { return '<span class="block text-xs text-stone-500 dark:text-stone-400">' + html + '</span>'; }

  /* What tells two selected athletes of the same name apart. */
  function tag(h) {
    return h.club || (h.by ? 'Jg. ' + h.by : '') || h.tv || (h.first ? String(h.first) + '–' + String(h.last) : '');
  }

  function sameNameCount(e) {
    return ok().filter(function (o) { return o.h.name === e.h.name; }).length;
  }

  /* Plain text (chart tooltip titles, aria labels): "Name (Klub)" for namesakes. */
  function plainName(e) {
    var t = sameNameCount(e) > 1 ? tag(e.h) : '';
    return e.h.name + (t ? ' (' + t + ')' : '');
  }

  /* Name as HTML with the identity marker; never without it. */
  function nameHtml(e) {
    return SE.esc(plainName(e)) + SE.uncertainMark(e.h.unc);
  }

  /* Column head of an athlete: colour key, surname and first name on two lines (narrow
   * columns on a phone), the identity marker, and what tells namesakes apart. */
  function head(e, link) {
    var cut = e.h.name.indexOf(' ');
    var first = cut === -1 ? e.h.name : e.h.name.slice(0, cut), rest = cut === -1 ? '' : e.h.name.slice(cut + 1);
    var name = '<span class="block">' + SE.esc(first) + '</span>' + (rest ? '<span class="block">' + SE.esc(rest) + SE.uncertainMark(e.h.unc) + '</span>' : SE.uncertainMark(e.h.unc));
    var t = sameNameCount(e) > 1 ? tag(e.h) : '';
    return '<th class="se-th"><span class="flex items-baseline gap-1">' + swatch(e) +
      (link ? '<a class="se-link text-sm font-semibold" href="' + SE.athleteUrl(e.h.id) + '">' + name + '</a>'
        : '<span class="font-semibold text-stone-900 dark:text-stone-100">' + name + '</span>') + '</span>' +
      (t ? muted(SE.esc(t)) : '') + (link ? badges(e.h) : '') + '</th>';
  }

  function badges(h) {
    var out = '';
    if (has(h, 'inactive')) { out += '<span class="block mt-0.5"><span class="se-badge se-badge-muted">nicht mehr aktiv</span></span>'; }
    if (has(h, 'few_bouts')) { out += '<span class="block mt-0.5"><span class="se-badge se-badge-muted">provisorisch</span></span>'; }
    return out;
  }

  function span(e) {
    var r = e.rows;
    return r.length ? [r[0].date, r[r.length - 1].date] : null;
  }

  // ------------------------------------------------------------------ selection bar
  function renderPicked() {
    var html = '';
    entries.forEach(function (e) {
      if (e.status !== 'ok') { return; }
      html += '<span class="se-chip flex items-center gap-1">' + swatch(e) + '<span>' + nameHtml(e) + '</span>' +
        '<a class="se-link font-bold" href="' + SE.esc(compareUrl(without(e.id))) + '" data-remove="' + SE.esc(e.id) +
        '" title="Aus dem Vergleich entfernen" aria-label="' + SE.esc(plainName(e)) + ' aus dem Vergleich entfernen">×</a></span>';
    });
    picked.innerHTML = html;
    var full = entries.length >= MAX;
    input.disabled = full;
    input.placeholder = full ? 'Höchstens ' + MAX + ' Schwinger' : 'Schwinger hinzufügen (Name, Klub)';
    if (full) { closePanel(); }
    hint.textContent = full ? 'Es lassen sich höchstens ' + MAX + ' Schwinger vergleichen. Zuerst einen entfernen (×), um einen anderen hinzuzufügen.' : '';
  }

  /* Ids can change when the data are rebuilt: per outdated slot a search from the words
   * of the id, each hit a link that puts the athlete into that slot. */
  function renderSlots() {
    var html = '';
    if (dropped.invalid) {
      html += SE.note(SE.num(dropped.invalid) + (dropped.invalid === 1 ? ' Angabe im Link ist' : ' Angaben im Link sind') +
        ' keine gültige Kennung und wurde' + (dropped.invalid === 1 ? '' : 'n') + ' ignoriert.');
    }
    if (dropped.over) {
      html += SE.note('Es lassen sich höchstens ' + MAX + ' Schwinger vergleichen; ' + SE.num(dropped.over) +
        (dropped.over === 1 ? ' weiterer im Link wurde' : ' weitere im Link wurden') + ' weggelassen.', 'info');
    }
    entries.forEach(function (e, i) {
      if (e.status === 'error') {
        /* which athlete of the link it concerns, in words - never the parser's message */
        var code = e.error && e.error.status ? ' (Fehler ' + SE.esc(e.error.status) + ' des Servers)' : '';
        html += '<div class="se-note"><strong>Daten nicht geladen.</strong> Die Daten zur Kennung «' + SE.esc(e.id) +
          '» in diesem Link konnten nicht gelesen werden' + code + ': die Verbindung wurde unterbrochen oder die Datei ist unvollständig. ' +
          'Dieser Schwinger fehlt deshalb im Vergleich; die übrigen sind vollständig. Bitte die Seite neu laden. ' +
          '<a class="se-link" href="' + SE.esc(compareUrl(without(e.id))) + '" data-remove="' + SE.esc(e.id) + '">Aus dem Vergleich entfernen</a></div>';
      }
      if (e.status !== 'missing') { return; }
      html += '<div class="se-note"><strong>Schwinger nicht gefunden.</strong> Zur Kennung «' + SE.esc(e.id) +
        '» in diesem Link gibt es kein Profil (mehr). Die Kennungen können sich ändern, wenn die Daten neu aufgebaut werden. ' +
        '<a class="se-link" href="' + SE.esc(compareUrl(without(e.id))) + '" data-remove="' + SE.esc(e.id) + '">Aus dem Vergleich entfernen</a>' +
        '<div class="mt-2" data-suggest="' + i + '"></div></div>';
    });
    slots.innerHTML = html;
    entries.forEach(function (e, i) {
      if (e.status === 'missing') { suggest(e, i); }
    });
  }

  function suggest(e, i) {
    var words = SE.norm(e.id.replace(/-p?\d+(-\d+)?$/, '').replace(/-/g, ' '));
    if (!words) { return; }
    SE.loadSearchIndex().then(function (index) {
      var box = slots.querySelector('[data-suggest="' + i + '"]');
      if (!box || entries[i] !== e) { return; }
      var taken = {};
      ids().forEach(function (id) { taken[id] = true; });
      function hits(q) {
        return SE.search(index, q, 40).hits.filter(function (a) { return !taken[a.id]; }).slice(0, 5);
      }
      var res = hits(words);
      if (!res.length) { res = hits(words.split(' ')[0]); }
      if (!res.length) { return; }
      var html = '<span class="font-medium">Vielleicht gemeint:</span><ul class="mt-1 space-y-1">';
      res.forEach(function (a) {
        var list = ids();
        list[i] = a.id;
        html += '<li><a class="se-link font-medium" href="' + SE.esc(compareUrl(list)) + '" data-replace="' + i + '" data-id="' + SE.esc(a.id) + '">' +
          SE.esc(a.name) + '</a>' + ((a.flags & SE.F_UNCERTAIN) ? SE.uncertainMark(1) : '') +
          ' <span class="text-xs">(' + (SE.subline(a, true) || 'ohne weitere Angaben') + ')</span></li>';
      });
      box.innerHTML = html + '</ul>';
    }).catch(function () { /* the picker still works */ });
  }

  // ------------------------------------------------------------------ picker
  var seq = 0;

  function closePanel() { panel.classList.add('hidden'); }

  function runPicker() {
    var q = input.value, mine = ++seq;
    if (!SE.norm(q)) { closePanel(); return; }
    panel.classList.remove('hidden');
    panel.innerHTML = '<p class="p-3 text-sm text-stone-500 dark:text-stone-400">Suche wird geladen …</p>';
    SE.loadSearchIndex().then(function (index) {
      if (mine !== seq) { return; }
      var taken = {};
      ids().forEach(function (id) { taken[id] = true; });
      var res = SE.search(index, q, 60);
      var hits = res.hits.filter(function (a) { return !taken[a.id]; }).slice(0, 20);
      if (!hits.length) {
        panel.innerHTML = '<p class="p-3 text-sm text-stone-500 dark:text-stone-400">' +
          (res.total ? 'Bereits im Vergleich.' : 'Kein Schwinger gefunden.') + '</p>';
        return;
      }
      /* Same rows as the search of the header: club, birth year, Teilverband and years
       * tell namesakes apart; a hit adds the athlete instead of opening his profile. */
      var html = '<ul>';
      hits.forEach(function (a) {
        html += '<li><a class="se-hit" href="' + SE.esc(compareUrl(ids().concat([a.id]))) + '" data-add="' + SE.esc(a.id) + '">' +
          '<span class="block font-medium">' + SE.esc(a.name) + ((a.flags & SE.F_UNCERTAIN) ? SE.uncertainMark(1) : '') + '</span>' +
          muted(SE.subline(a, true)) + muted(SE.esc(SE.searchStatus(a))) + '</a></li>';
      });
      html += '</ul>';
      if (res.total > hits.length + ids().length) {
        html += '<p class="px-3 py-2 text-xs text-stone-500 dark:text-stone-400">Weitere Treffer – Suche verfeinern (z.B. Klub ergänzen).</p>';
      }
      panel.innerHTML = html;
    }).catch(function (err) {
      if (mine === seq) { SE.showError(panel, err); }
    });
  }

  function add(id) {
    if (!SE.ID_RE.test(id) || entries.length >= MAX || ids().indexOf(id) !== -1) { return; }
    input.value = '';
    closePanel();
    setIds(ids().concat([id]), true);
  }

  input.addEventListener('focus', function () { SE.loadSearchIndex().catch(function () {}); runPicker(); });
  input.addEventListener('input', runPicker);
  input.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') { closePanel(); input.blur(); }
    if (ev.key === 'Enter') {
      var first = panel.querySelector('[data-add]');
      if (first && !panel.classList.contains('hidden')) { add(first.getAttribute('data-add')); }
    }
  });

  /* The links carry a real address (open in a new tab, copy), a plain click changes the
   * selection in place. */
  document.addEventListener('click', function (ev) {
    var t = ev.target, el = t && t.closest ? t.closest('[data-add],[data-remove],[data-replace]') : null;
    if (!SE.$('se-pick').contains(t)) { closePanel(); }
    if (!el || ev.defaultPrevented || ev.button || ev.metaKey || ev.ctrlKey || ev.shiftKey) { return; }
    ev.preventDefault();
    if (el.hasAttribute('data-add')) {
      add(el.getAttribute('data-add'));
    } else if (el.hasAttribute('data-remove')) {
      setIds(without(el.getAttribute('data-remove')), true);
    } else {
      var list = ids(), id = el.getAttribute('data-id');
      if (SE.ID_RE.test(id) && list.indexOf(id) === -1) {
        list[Number(el.getAttribute('data-replace'))] = id;
        setIds(list, true);
      }
    }
  });

  // ------------------------------------------------------------------ figures
  function figures(list) {
    function row(label, cell, title, wrap) {
      var html = '<tr><th class="se-th"' + (title ? ' title="' + title + '"' : '') + '>' + label + '</th>';
      list.forEach(function (e) { html += '<td class="se-td tabular-nums' + (wrap ? '' : ' whitespace-nowrap') + '">' + cell(e.h, e) + '</td>'; });
      return html + '</tr>';
    }
    var html = '<h2 class="text-lg font-bold">Kennzahlen</h2>' +
      '<div class="mt-2 overflow-x-auto"><table class="se-table"><thead><tr><th class="se-th"><span class="sr-only">Kennzahl</span></th>';
    list.forEach(function (e) { html += head(e, true); });
    html += '</tr></thead><tbody>';
    /* As on the profile: an idle athlete's stored rating keeps moving towards 1500, so
     * his last rating after a festival is shown instead and said to be that. */
    html += row('Wertung', function (h) {
      if (has(h, 'inactive')) {
        return '<span class="font-semibold">' + SE.rating(h.rating_last) + '</span>' + muted('letzte Wertung, ' + SE.esc(SE.date(h.last_date)));
      }
      return '<span class="font-semibold">' + SE.rating(h.rating) + '</span>' +
        muted(h.ranked ? 'Rang ' + SE.num(h.rank) : 'ohne Rang');
    }, 'Aktuelle Wertung am Datenstand; bei nicht mehr Aktiven die Wertung nach dem letzten Fest');
    html += row('Bestwert', function (h) {
      return h.peak === null ? '–' : '<span class="font-semibold">' + SE.rating(h.peak) + '</span>' + muted(SE.esc(SE.date(h.peak_date)));
    }, 'Höchste Wertung nach einem Fest (ab der Saison 2012, erst ab genügend Gängen)');
    html += row('Letzter Kampf', function (h) {
      return SE.esc(SE.date(h.last_date)) +
        (h.idle > IDLE_WARN_DAYS ? '<span class="block text-xs"><span class="se-idle" title="Tage ohne Kampf bis zum Datenstand">' + SE.num(h.idle) + ' Tage</span></span>' : '');
    }, 'Letzter erfasster Kampf; hervorgehoben, wenn er am Datenstand mehr als 180 Tage zurücklag');
    html += row('Erfasste Jahre', function (h) {
      return (h.first ? SE.esc(h.first) + (h.first === h.last ? '' : '–' + SE.esc(h.last)) : '–') + muted(SE.num(h.festivals) + (h.festivals === 1 ? ' Fest' : ' Feste'));
    }, 'Erstes und letztes Jahr mit einem erfassten Fest (die Daten beginnen 2011)');
    html += row('Gänge', function (h) { return SE.num(h.bouts); }, 'Gewertete Gänge');
    html += row('Bilanz', function (h) {
      return h.record ? SE.num(h.record[0]) + '–' + SE.num(h.record[1]) + '–' + SE.num(h.record[2]) : '–';
    }, 'gewonnen – gestellt – verloren');
    html += row('Klub', function (h) { return h.club ? SE.esc(h.club) : '–'; }, '', true);
    html += row('Teilverband', function (h) {
      return h.tv ? '<abbr title="' + SE.esc(SE.TV[h.tv] || h.tv) + '">' + SE.esc(h.tv) + '</abbr>' : '–';
    });
    html += row('Jahrgang', function (h) { return h.by ? SE.esc(h.by) : '–'; });
    html += '</tbody></table></div>' +
      '<p class="mt-2 text-xs text-stone-500 dark:text-stone-400">Bilanz: gewonnen – gestellt – verloren. Klub: häufigster Klub der Laufbahn, nicht zwingend der heutige; «–» heisst, die Angabe fehlt in den Quellen.</p>';
    return html;
  }

  /* Where the comparison invites a reading the numbers do not support, say so. */
  function caveats(list) {
    var out = [];
    list.forEach(function (e) {
      if (e.h.unc) {
        out.push(SE.note('<strong>Identität unsicher:</strong> ' + SE.esc(plainName(e)) + '. Bei ' + SE.num(e.h.unc_rows[0]) + ' von ' + SE.num(e.h.unc_rows[1]) +
          ' Festen liess sich nicht sicher entscheiden, welcher Namensvetter angetreten ist. Verlauf, Bilanz und direkte Gänge können Gänge einer anderen Person enthalten.'));
      }
    });
    scaleNoted = false;
    if (list.length < 2) { return out.join(''); }
    /* by season: two careers that touch in one year are not "apart" */
    var spans = list.map(function (e) {
      var s = span(e);
      return s ? [s[0].slice(0, 4), s[1].slice(0, 4)] : null;
    }), apart = [];
    function years(s) { return s[0] === s[1] ? s[0] : s[0] + '–' + s[1]; }
    for (var i = 0; i < list.length; i++) {
      for (var j = i + 1; j < list.length; j++) {
        if (spans[i] && spans[j] && (spans[i][1] < spans[j][0] || spans[j][1] < spans[i][0])) {
          apart.push(SE.esc(plainName(list[i])) + ' (' + SE.esc(years(spans[i])) + ') und ' +
            SE.esc(plainName(list[j])) + ' (' + SE.esc(years(spans[j])) + ')');
        }
      }
    }
    if (apart.length) {
      out.push(SE.note('<strong>Keine gemeinsame Zeit:</strong> ' + apart.join('; ') +
        '. Ihre erfassten Laufbahnen überschneiden sich nicht – wer stärker war, lässt sich aus den Wertungen nicht ablesen.'));
    }
    /* The scale caveat only where it changes the reading of this comparison: best values
     * from both sides of 2016, or somebody whose whole career lies in the years of the
     * growing scale next to somebody with later ratings. A career that merely began
     * before 2016 (most long ones did) is compared at the same dates and needs no note. */
    var peaks = list.filter(function (e) { return e.h.peak_date; }).map(function (e) { return e.h.peak_date; });
    var mixed = peaks.some(function (d) { return d < SCALE_SETTLED; }) && peaks.some(function (d) { return d >= SCALE_SETTLED; });
    var ended = list.some(function (e) { return e.rows.length && e.rows[e.rows.length - 1].date < SCALE_SETTLED; });
    var late = list.some(function (e) { return e.rows.length && e.rows[e.rows.length - 1].date >= SCALE_SETTLED; });
    if (mixed || (ended && late)) {
      scaleNoted = true;
      out.push(SE.note((mixed ? '<strong>Bestwerte aus verschiedenen Jahren sind nicht direkt vergleichbar.</strong> ' : '') +
        'Die Skala wächst bis etwa 2016 noch an: Wertungen aus den Jahren 2011 bis 2015 liegen systematisch tiefer als spätere, ' +
        'unabhängig davon, wer besser war. Verlässlich vergleichen lässt sich, wer zur selben Zeit höher stand. ' +
        '<a class="se-link" href="about.html#grenzen">Mehr dazu</a>', 'info'));
    }
    return out.join('');
  }

  // ------------------------------------------------------------------ chart
  function chartColours() {
    return isDark()
      ? { text: '#d6d3d1', grid: '#44403c', muted: '#a8a29e', bg: '#1c1917', border: '#57534e' }
      : { text: '#44403c', grid: '#e7e5e4', muted: '#78716c', bg: '#ffffff', border: '#d6d3d1' };
  }

  /* [first date of the latest starter, last date of the earliest finisher] or null. */
  function overlap(list) {
    var a = '', b = '9999';
    list.forEach(function (e) {
      var s = span(e);
      if (!s) { return; }
      if (s[0] > a) { a = s[0]; }
      if (s[1] < b) { b = s[1]; }
    });
    return a && a < b ? [a, b] : null;
  }

  /* Bout axis: x = number of rated bouts since the first recorded festival (the sum of
   * the history rows' n), one point per festival at the count after its last bout. The
   * rating is only known before and after a festival, so the points are joined by
   * straight segments. Where the next festival starts from another rating than the last
   * one ended with, the 1 April reversion lay in between: the line is broken there and
   * the difference drawn as its own dashed vertical step, as in the time chart. */
  function boutSeries(e) {
    var line = [], reversion = [], points = [], c = 0, prev = null;
    e.rows.forEach(function (r) {
      var gap = prev !== null && Math.abs(r.before - prev.after) > 1e-9;
      if (gap) {
        line.push([c, null]);
        reversion.push([c, prev.after], [c, r.before], [c, null]);
      }
      if (gap || prev === null) { line.push([c, r.before]); }
      c += r.n;
      line.push([c, r.after]);
      points.push({ value: [c, r.after], row: r, from: c - r.n + 1, to: c });
      prev = r;
    });
    return { line: line, reversion: reversion, points: points, tail: [], lo: 0, hi: c };
  }

  /* The bouts file carries the contribution per bout (older cached copies do not). */
  function gangReady(e) {
    return !!(e.bouts && e.bouts.cols && e.bouts.cols.indexOf('d') === 6 && e.bouts.names);
  }

  /* Bout axis with the Gänge of every festival. The engine rates a festival as a whole:
   * every bout counts against the ratings *before* the festival, and only the sum becomes
   * the new rating. So there is no rating "after Gang 3"; what exists is each bout's
   * contribution (d of the bouts file). Inside a festival the line is
   * before + contributions in Gang order - a breakdown of the festival's change.
   *
   * Bouts against athletes who are not published have no row in the bouts file. Their
   * number and their combined contribution follow from the history row
   * (n - listed bouts, after - before - listed contributions); they are drawn as one
   * dotted stretch at the end of the festival, wherever they really took place.
   * The festival points are the ones of boutSeries (same x, same y). */
  function gangSeries(e) {
    var line = [], reversion = [], rest = [], points = [], gangs = [], c = 0, prev = null, unsure = 0;
    var byFest = {};
    e.bouts.fests.forEach(function (f) {
      byFest[f[0]] = f[1].filter(function (g) { return g[6] !== null && g[6] !== undefined; });
    });
    e.rows.forEach(function (r) {
      var gap = prev !== null && Math.abs(r.before - prev.after) > 1e-9;
      if (gap) {
        line.push([c, null]);
        reversion.push([c, prev.after], [c, r.before], [c, null]);
      }
      if (gap || prev === null) { line.push([c, r.before]); }
      var list = byFest[r.fest_id] || [];
      if (list.length > r.n) { list = []; }          // files of different builds: festival only
      var hidden = r.n - list.length, v = r.before, sum = 0, seen = {}, doubt = false;
      list.forEach(function (g) {
        if ((g[5] & B_GANG_UNCERTAIN) || seen[g[0]]) { doubt = true; }
        seen[g[0]] = true;
      });
      if (doubt) { unsure++; }
      var items = list.map(function (g, i) {
        sum += g[6];
        /* without hidden bouts the last Gang ends at the festival's rating (the single
         * contributions are rounded) */
        v = (hidden === 0 && i === list.length - 1) ? r.after : r.before + sum;
        var item = { gang: g[0], opp: g[1], res: g[2], g: g[3], go: g[4], flags: g[5], d: g[6], v: v };
        var x = c + i + 1;
        line.push([x, v]);
        if (hidden > 0 || i < list.length - 1) {      // the last one is the festival point
          gangs.push({ value: [x, v], item: item, row: r, doubt: doubt });
        }
        return item;
      });
      var end = c + r.n, restInfo = null;
      if (hidden > 0) {
        restInfo = { n: hidden, d: r.after - r.before - sum };
        line.push([c + list.length, null]);
        rest.push([c + list.length, v], [end, r.after], [end, null]);
        line.push([end, r.after]);
      }
      points.push({ value: [end, r.after], row: r, from: c + 1, to: end, items: items, rest: restInfo, doubt: doubt });
      c = end;
      prev = r;
    });
    return { line: line, reversion: reversion, rest: rest, points: points, gangs: gangs, tail: [], lo: 0, hi: c, unsure: unsure, perGang: true };
  }

  function hasBirthYear(e) { return typeof e.h.by === 'number' && e.h.by > 1800; }

  /* ISO date -> calendar year plus the elapsed share of that year. */
  function yearPos(d) {
    var y = Number(d.slice(0, 4)), a = Date.UTC(y, 0, 1);
    return y + (Date.parse(d) - a) / (Date.UTC(y + 1, 0, 1) - a);
  }

  /* Age axis: the time chart moved by the birth year, nothing else. Only the year of
   * birth is known, so x is "calendar year minus birth year" plus the position inside
   * the calendar year: the stretch from 22 to 23 is the year in which he turns 22. */
  function ageSeries(e) {
    var s = e.series || (e.series = SE.careerSeries(e.h)), by = e.h.by;
    function at(p) { return [yearPos(p[0]) - by, p[1]]; }
    var points = s.points.map(function (p) {
      return { value: at(p.value), row: p.row, age: Number(p.row.date.slice(0, 4)) - by };
    });
    return {
      line: s.line.map(at), reversion: s.reversion.map(at), tail: s.tail.map(at), points: points,
      lo: points.length ? points[0].value[0] : 0, hi: points.length ? points[points.length - 1].value[0] : 0
    };
  }

  /* Career-season axis: x = 1 for the first recorded season, counted by calendar year
   * (a season without a recorded festival has no point and no rating, as its field in
   * the season table is empty, but it still counts as a year of the career); y = the
   * season-end rating of the history file. The stretch over such a hole is its own
   * dashed series. */
  function seasonSeries(e) {
    var rows = SE.table(e.h.seasons), line = [], gaps = [], points = [], prev = null;
    var first = rows.length ? rows[0].season : 0;
    rows.forEach(function (s) {
      var x = s.season - first + 1;
      if (prev && x - prev[0] > 1) {
        line.push([prev[0], null]);
        gaps.push(prev, [x, s.rating], [x, null]);
      }
      line.push([x, s.rating]);
      points.push({ value: [x, s.rating], season: s, k: x });
      prev = [x, s.rating];
    });
    return { line: line, reversion: gaps, points: points, tail: [], lo: 1, hi: prev ? prev[0] : 1 };
  }

  function seriesOf(e) {
    var x = ui.x;
    if (x === 'time') { return e.series || (e.series = SE.careerSeries(e.h)); }
    if (!e.alt) { e.alt = {}; }
    if (x === 'bouts' && gangReady(e)) { x = 'gang'; }
    if (!e.alt[x]) {
      e.alt[x] = x === 'gang' ? gangSeries(e) : x === 'bouts' ? boutSeries(e) : x === 'age' ? ageSeries(e) : seasonSeries(e);
    }
    return e.alt[x];
  }

  /* The athletes the chart can show: on the age axis only those with a birth year. */
  function drawable(list) {
    return ui.x === 'age' ? list.filter(hasBirthYear) : list;
  }

  /* The stretch of the axis every drawn athlete covers (bouts, age, career season), with
   * the labels of the two zoom buttons; null when there is nothing to narrow down. */
  function sharedRange(list) {
    if (ui.x === 'time' || list.length < 2) { return null; }
    var lo = -Infinity, hi = Infinity, top = -Infinity, low = Infinity;
    list.forEach(function (e) {
      var s = seriesOf(e);
      lo = Math.max(lo, s.lo); hi = Math.min(hi, s.hi);
      low = Math.min(low, s.lo); top = Math.max(top, s.hi);
    });
    if (!(lo < hi) || (lo === low && hi === top)) { return null; }
    if (ui.x === 'bouts') {
      return { from: lo, to: hi, all: 'Alle Gänge', label: 'Gemeinsamer Bereich bis Gang ' + SE.num(hi), aria: 'Bereich der Gänge' };
    }
    if (ui.x === 'season') {
      return { from: lo, to: hi, all: 'Alle Saisons', label: 'Gemeinsame Saisons 1–' + SE.esc(hi), aria: 'Bereich der Saisons' };
    }
    /* whole years only; an overlap inside one calendar year is that one age, not "22–22" */
    var a = Math.floor(lo), b = Math.floor(hi);
    return { from: lo, to: hi, all: 'Ganzes Alter', label: 'Gemeinsames Alter ' + SE.esc(a) + (b > a ? '–' + SE.esc(b) : ''), aria: 'Bereich des Alters' };
  }

  var MIN_SEASONS = 5;

  /* The Gang points of the chart on the page (series id and data) and whether they are
   * shown at the moment. */
  var gangPoints = [], gangDots = false;

  /* How many bouts the visible range may span for single Gänge to get their own point. */
  function dotLimit() {
    var el = SE.$('se-chart'), w = el ? el.clientWidth - 58 : 300;
    return Math.max(40, Math.floor(w / DOT_PX));
  }

  /* FIRST_BOUTS when the button "Erste n Gänge" makes sense: Gänge are drawn and somebody
   * has more bouts than that. */
  function firstBouts(list) {
    if (ui.x !== 'bouts') { return 0; }
    var any = false, top = 0;
    list.forEach(function (e) {
      var s = seriesOf(e);
      any = any || !!s.perGang;
      top = Math.max(top, s.hi);
    });
    return any && top > FIRST_BOUTS ? FIRST_BOUTS : 0;
  }

  /* "+12.7" / "−3.4": a contribution in rating points, one decimal as exported. */
  function points1(d) {
    var r = Math.round(d * 10) / 10;
    return (r > 0 ? '+' : r < 0 ? '−' : '±') + SE.esc(Math.abs(r).toFixed(1));
  }

  var RESULT = { 0: 'Gestellt gegen', 1: 'Sieg gegen', 2: 'Niederlage gegen' };

  /* "Sieg gegen Muster Hans ?" - the opponent is a published athlete (the bouts file
   * holds no others), named from the file's own list. */
  function gangText(e, it) {
    var unc = e.bouts.unc && e.bouts.unc.indexOf(it.opp) !== -1;
    return (RESULT[it.res] || 'Gegen') + ' ' + SE.esc(e.bouts.names[it.opp] || 'Gegner') + (unc ? ' ?' : '') +
      ((it.flags & B_SCHLUSSGANG) ? ' (Schlussgang)' : '');
  }

  function restText(rest) {
    return SE.num(rest.n) + (rest.n === 1 ? ' Gang gegen einen nicht veröffentlichten Gegner: ' : ' Gänge gegen nicht veröffentlichte Gegner: zusammen ') + points1(rest.d);
  }

  var DOUBT = 'Reihenfolge der Gänge an diesem Fest nicht gesichert';

  function seasonLabel(k) { return SE.esc(k) + '. erfasste Saison'; }

  function chartOption(list) {
    var c = chartColours(), series = [], owners = [], mode = ui.x;
    var total = 0;
    list.forEach(function (e) { total += seriesOf(e).points.length; });
    var size = mode === 'season' ? (total > 60 ? 5 : 6.5) : total > 400 ? 3 : total > 150 ? 4 : 6.5;
    /* a highlighted athlete who is not drawn on this axis dims nobody */
    var focus = list.some(function (e) { return e.id === ui.focus; }) ? ui.focus : null;
    list.forEach(function (e, i) {
      var s = seriesOf(e), col = colour(e);
      var dim = focus !== null && focus !== e.id, on = focus === e.id;
      var op = dim ? 0.16 : 1, z = dim ? 1 : on ? 6 : 3;
      /* As on the profile: a festival is a vertical step of the solid line; the 1 April
       * reversion is its own dashed step between two festivals and belongs to none. */
      var line = {
        name: e.id, type: 'line', data: s.line, showSymbol: false, silent: true, z: z, connectNulls: false,
        lineStyle: { color: col, width: on ? 2.4 : 1.5, opacity: op }, itemStyle: { color: col }
      };
      if (i === 0) {
        line.markLine = {
          silent: true, symbol: 'none', lineStyle: { color: c.muted, type: 'dotted' },
          label: { formatter: 'Start 1500', color: c.muted, fontSize: 10, position: 'insideEndTop' },
          data: [{ yAxis: e.h.rev[1] }]
        };
      }
      series.push(line);
      owners.push(e);
      series.push({
        name: e.id + ' 1.4.', type: 'line', data: s.reversion, showSymbol: false, silent: true, z: z, connectNulls: false,
        lineStyle: { color: col, width: 1.2, type: [2, 2], opacity: op }, itemStyle: { color: col }
      });
      owners.push(e);
      if (s.tail.length && e.h.idle > 60) {
        series.push({
          name: e.id + ' ohne Kampf', type: 'line', data: s.tail, showSymbol: false, silent: true, z: z,
          lineStyle: { color: col, width: 1.2, type: 'dotted', opacity: op * 0.8 }, itemStyle: { color: col }
        });
        owners.push(e);
      }
      if (s.rest && s.rest.length) {
        /* bouts against athletes who are not published: combined, dotted */
        series.push({
          name: e.id + ' Rest', type: 'line', data: s.rest, showSymbol: false, silent: true, z: z, connectNulls: false,
          lineStyle: { color: col, width: 1.2, type: 'dotted', opacity: op }, itemStyle: { color: col }
        });
        owners.push(e);
      }
      /* career seasons: a season without a place in the season ranking is a hollow point */
      var data = mode !== 'season' ? s.points : s.points.map(function (p) {
        return p.season.pos !== null ? p
          : { value: p.value, season: p.season, k: p.k, itemStyle: { color: c.bg, borderColor: col, borderWidth: 1.5, opacity: op } };
      });
      series.push({
        name: e.id + ' Feste', type: 'scatter', data: data, symbol: SYMBOLS[e.slot], symbolSize: size, z: z + 1,
        silent: dim, itemStyle: { color: col, opacity: op }, emphasis: { scale: 2.5 }
      });
      owners.push(e);
    });
    /* the single Gänge: small points, only while there is room for them (drawChart
     * switches them with the zoom); the festival points lie on top */
    gangPoints = [];
    list.forEach(function (e) {
      var s = seriesOf(e);
      if (!s.perGang) { return; }
      var dim = focus !== null && focus !== e.id;
      gangPoints.push({ id: 'gang-' + e.slot, data: s.gangs });
      series.push({
        id: 'gang-' + e.slot, name: e.id + ' Gänge', type: 'scatter', data: [], symbol: 'circle', symbolSize: 5,
        z: dim ? 1 : 4, silent: dim, emphasis: { scale: 2 },
        /* hollow, so that the filled festival points stay recognisable */
        itemStyle: { color: c.bg, borderColor: colour(e), borderWidth: 1.2, opacity: dim ? 0.16 : 1 }
      });
      owners.push(e);
    });
    var zoom = { type: 'slider', height: 20, bottom: 8, borderColor: c.border, textStyle: { color: c.muted },
      fillerColor: 'rgba(120,113,108,0.2)', brushSelect: false, showDataShadow: false };
    var both = mode === 'time' ? overlap(list) : null, shared = sharedRange(list);
    if (ui.zoom === 'common' && both) {
      zoom.startValue = Date.parse(both[0]);
      zoom.endValue = Date.parse(both[1]);
    }
    if (ui.zoom === 'common' && shared) {
      zoom.startValue = shared.from;
      zoom.endValue = shared.to;
    }
    if (ui.zoom === 'first' && firstBouts(list)) {
      zoom.startValue = 0;
      zoom.endValue = FIRST_BOUTS;
    }
    if (gangPoints.length) {
      var top = 0;
      list.forEach(function (e) { top = Math.max(top, seriesOf(e).hi); });
      gangDots = (zoom.endValue === undefined ? top : zoom.endValue - zoom.startValue) <= dotLimit();
      if (gangDots) {
        series.forEach(function (s) {
          gangPoints.forEach(function (g) { if (s.id === g.id) { s.data = g.data; } });
        });
      }
    }
    var xAxis = {
      type: 'time', axisLine: { lineStyle: { color: c.border } },
      axisLabel: { color: c.muted, hideOverlap: true }, splitLine: { show: false }
    };
    if (mode !== 'time') {
      xAxis.type = 'value';
      xAxis.minInterval = 1;
      /* career seasons: from 0 (no label: there is no season 0) over at least five seasons,
       * so a first season is never on the border of the chart */
      xAxis.min = mode === 'age' ? function (v) { return Math.floor(v.min); } : 0;
      xAxis.max = mode === 'age' ? function (v) { return Math.ceil(v.max); }
        : mode === 'season' ? function (v) { return Math.max(v.max, MIN_SEASONS); } : 'dataMax';
      xAxis.axisLabel.formatter = function (v) { return mode !== 'season' ? String(v) : v < 1 ? '' : String(v) + '.'; };
      zoom.labelFormatter = function (v) { return String(Math.round(v)); };
    }
    return {
      animation: false,
      textStyle: { color: c.text, fontFamily: 'inherit' },
      grid: { left: 44, right: 14, top: 12, bottom: 62 },
      tooltip: {
        trigger: 'item', confine: true, backgroundColor: c.bg, borderColor: c.border,
        textStyle: { color: c.text, fontSize: 12 },
        formatter: function (p) {
          var r = p.data && p.data.row, e = owners[p.seriesIndex], s = p.data && p.data.season;
          if (!e || (!r && !s)) { return ''; }
          var it = p.data.item;
          if (it) {
            /* one Gang: what it contributed, and the running sum - said to be that */
            return '<strong>' + SE.esc(plainName(e)) + (e.h.unc ? ' ?' : '') + '</strong>' +
              (e.h.unc ? ' <span>(Identität unsicher)</span>' : '') + '<br>' + SE.esc(r.fest) + '<br>' +
              SE.esc(SE.date(r.date)) + ' · ' + SE.esc(it.gang) + '. Gang<br>' + gangText(e, it) + '<br>Beitrag <strong>' + points1(it.d) +
              '</strong> · Zwischenstand ' + SE.rating(it.v) + '<br><span>(Aufteilung des Fests, keine eigene Wertung)</span>' +
              (p.data.doubt ? '<br>' + DOUBT : '');
          }
          var who = '<strong>' + SE.esc(plainName(e)) + (e.h.unc ? ' ?' : '') + '</strong>' +
            (e.h.unc ? ' <span>(Identität unsicher)</span>' : '') + '<br>';
          if (s) {
            return who + seasonLabel(p.data.k) + ' (' + SE.esc(s.season) + ')<br>Wertung am Saisonende <strong>' + SE.rating(s.rating) +
              '</strong><br>' + (s.pos === null ? 'ohne Platz' : 'Platz ' + SE.num(s.pos)) + ' · ' + SE.num(s.bouts) + (s.bouts === 1 ? ' Gang' : ' Gänge');
          }
          var extra = '';
          if (mode === 'bouts') {
            extra = '<br>' + (p.data.from < p.data.to ? 'Gang ' + SE.num(p.data.from) + '–' + SE.num(p.data.to) : 'Gang ' + SE.num(p.data.to)) +
              ' seiner erfassten Gänge';
          } else if (mode === 'age') {
            extra = '<br>Im Jahr seines ' + SE.esc(p.data.age) + '. Geburtstags (Jahrgang ' + SE.esc(e.h.by) + ')';
          }
          /* the festival point of the bout axis lists its Gänge (a finger hits the big
           * point more easily than the small ones) */
          var parts = '';
          if (p.data.items) {
            p.data.items.forEach(function (g) {
              parts += '<br>' + SE.esc(g.gang) + '. ' + gangText(e, g) + ': ' + points1(g.d);
            });
            if (p.data.rest) { parts += '<br>' + restText(p.data.rest); }
            if (p.data.doubt) { parts += '<br>' + DOUBT; }
          }
          return who + SE.esc(r.fest) + '<br>' +
            SE.esc(SE.date(r.date)) + ' · ' + SE.esc(SE.category(r.cat)) + extra + '<br>Wertung ' + SE.rating(r.before) + ' → <strong>' +
            SE.rating(r.after) + '</strong> (' + SE.signed(r.after - r.before) + ')<br>' + SE.num(r.score, 1) + ' Punkte aus ' +
            SE.esc(r.n) + ' Gängen, erwartet ' + SE.num(r.exp, 1) + parts;
        }
      },
      xAxis: xAxis,
      yAxis: {
        type: 'value', scale: true,
        axisLabel: { color: c.muted, formatter: function (v) { return String(v); } },
        splitLine: { lineStyle: { color: c.grid } }
      },
      dataZoom: [zoom],
      series: series
    };
  }

  /* The axes as real links (shareable address); a plain click switches in place. */
  var AXES = [['time', 'Zeit'], ['bouts', 'Gänge'], ['age', 'Alter'], ['season', 'Karrieresaison']];

  function axisSwitch() {
    var html = '<div class="mt-2 flex flex-wrap items-center gap-2 text-xs" role="group" aria-label="Waagrechte Achse des Diagramms">' +
      '<span class="text-stone-500 dark:text-stone-400">Achse:</span>';
    AXES.forEach(function (a) {
      var on = ui.x === a[0];
      html += '<a class="se-chip' + (on ? ' se-chip-on' : '') + '" href="' + SE.esc(compareUrl(ids(), a[0])) + '" data-x="' + a[0] + '"' +
        (on ? ' aria-current="true"' : '') + '>' + a[1] + '</a>';
    });
    return html + '</div>';
  }

  function names(list) { return list.map(function (e) { return SE.esc(plainName(e)); }).join(', '); }

  /* Year of the first history row: where every curve of the chart starts. */
  function startYear(e) { return e.rows.length ? Number(e.rows[0].date.slice(0, 4)) : null; }

  /* The athletes whose record probably does not begin with their career. Two signs, either
   * is enough: the first festival lies in the first seasons of the data (they are thin, so
   * a "debut" there is often only the first sheet that was found), or it lies in the year
   * of his 20th birthday or later (active athletes usually start in their teens). Neither
   * proves earlier bouts; the note says "likely". Without meta.json only the second sign
   * can be read. */
  function likelyTruncated(list) {
    return list.filter(function (e) {
      var y = startYear(e);
      if (y === null) { return false; }
      return (firstSeason !== null && y < firstSeason + EARLY_SEASONS) || (hasBirthYear(e) && y - e.h.by >= ADULT_AGE);
    });
  }

  /* "Name (erstes erfasstes Fest 2013, im Jahr seines 24. Geburtstags)" */
  function startText(e) {
    var y = startYear(e);
    return SE.esc(plainName(e)) + ' (erstes erfasstes Fest ' + SE.esc(y) +
      (hasBirthYear(e) && y - e.h.by >= ADULT_AGE ? ', im Jahr seines ' + SE.esc(y - e.h.by) + '. Geburtstags' : '') + ')';
  }

  /* Always: the axis aligns different years. The scale caveat only when somebody drawn
   * has a festival from the years of the growing scale, and not a second time when the
   * scale note stands above the chart. */
  function eras(shown) {
    var old = shown.some(function (e) { return e.rows.length && e.rows[0].date < SCALE_SETTLED; });
    return 'Gleiche Stelle auf der Achse heisst nicht gleiche Zeit' + (!old ? '.'
      : scaleNoted ? ' (siehe den Hinweis zur Skala oben).'
        : ': Wertungen aus den Jahren vor etwa 2016 liegen systematisch tiefer (<a class="se-link" href="about.html#grenzen">mehr dazu</a>).');
  }

  /* Caption and caveats of the three axes that are not calendar time; a caveat only
   * when it applies to the selection. */
  function axisNotes(list, shown) {
    var p = '<p class="mt-1 text-xs text-stone-500 dark:text-stone-400">', early = likelyTruncated(shown), html = '';
    var one = early.length === 1;
    /* what is known, and what is only likely */
    var begin = (firstSeason !== null ? 'Die Daten beginnen ' + SE.esc(firstSeason) + ' und sind in den ersten Jahren lückenhaft. ' : '') +
      early.map(startText).join(', ') + ': ';
    if (ui.x === 'bouts' && shown.some(function (e) { return seriesOf(e).perGang; })) {
      var unsure = 0, hidden = false;
      var without = shown.filter(function (e) {
        var s = seriesOf(e);
        unsure += s.unsure || 0;
        hidden = hidden || !!(s.rest && s.rest.length);
        return !s.perGang;
      });
      html = p + 'Waagrecht: Anzahl gewerteter Gänge seit dem ersten erfassten Fest – so stehen die Laufbahnen nach Erfahrung nebeneinander statt nach Datum. ' +
        '<strong>Die Wertung wird pro Fest berechnet, nicht pro Gang:</strong> Alle Gänge eines Fests zählen gegen die Wertungen vor dem Fest, und erst nach dem Fest gilt die neue Wertung. ' +
        'Die Linie innerhalb eines Fests zeigt, wie sich dessen Änderung auf die Gänge verteilt (Wertung vor dem Fest plus die Beiträge der Gänge der Reihe nach) – ' +
        'ein Zwischenstand dieser Aufteilung, keine Wertung, gegen die der nächste Gegner gerechnet wurde. ' +
        'Grosser Punkt = Wertung nach einem Fest (antippen: alle Gänge des Fests mit Gegner, Ausgang und Beitrag). ' +
        'Kleine hohle Punkte = einzelne Gänge; sie erscheinen, sobald der Bereich unten eng genug gewählt ist' + (firstBouts(shown) ? ' (z.B. «Erste ' + FIRST_BOUTS + ' Gänge»)' : '') + '. ' +
        (hidden ? 'Gepunktet: Gänge gegen Schwinger, die nicht mit Namen veröffentlicht werden – nur zusammengefasst und am Ende des Fests eingetragen, unabhängig davon, wann sie stattfanden. ' : '') +
        'Gestrichelt senkrecht: Rückführung Richtung 1500 am Saisonwechsel (1. April) zwischen zwei Festen, gehört zu keinem Fest. ' +
        'Beiträge sind auf eine Dezimale gerundet. Alle beginnen beim Startwert 1500. ' + eras(shown) + '</p>';
      if (unsure) {
        html += '<div class="mt-2">' + SE.note('<strong>Reihenfolge der Gänge nicht überall gesichert.</strong> Bei ' + SE.num(unsure) +
          (unsure === 1 ? ' Fest' : ' Festen') + ' der Auswahl ist die Nummer eines Gangs in der Quelle unsicher oder doppelt vergeben. ' +
          'Der Verlauf innerhalb dieser Feste kann anders gewesen sein (im Hinweis zum Fest vermerkt); die Wertung nach dem Fest hängt nicht von der Reihenfolge ab.', 'info') + '</div>';
      }
      if (without.length) {
        html += '<div class="mt-2">' + SE.note('Die Gänge von ' + names(without) + ' konnten nicht geladen werden: dort ein Punkt pro Fest. Bitte die Seite neu laden.') + '</div>';
      }
    }
    if (ui.x === 'bouts' && !html) {
      html = p + 'Waagrecht: Anzahl gewerteter Gänge seit dem ersten erfassten Fest – so stehen die Laufbahnen nach Erfahrung nebeneinander statt nach Datum. ' +
        'Punkt = Wertung nach einem Fest, eingetragen bei seinem letzten Gang (antippen für Details); die Wertung ändert sich nur von Fest zu Fest, die Linie verbindet die Punkte. ' +
        'Gestrichelt senkrecht: Rückführung Richtung 1500 am Saisonwechsel (1. April) zwischen zwei Festen, gehört zu keinem Fest. ' +
        'Alle beginnen beim Startwert 1500. ' + eras(shown) + ' Unten lässt sich der Bereich eingrenzen.</p>';
    }
    if (ui.x === 'bouts') {
      if (early.length) {
        html += '<div class="mt-2">' + SE.note('<strong>«Gang 1» ist der erste erfasste Gang, nicht zwingend der erste der Laufbahn.</strong> ' + begin +
          'Wahrscheinlich ' + (one ? 'hatte er' : 'hatten sie') + ' schon Gänge davor, die nicht erfasst sind – sicher ist das nicht. ' +
          'Zählung und Startwert 1500 beginnen in jedem Fall beim ersten erfassten Fest; die ersten Gänge im Diagramm zeigen dann nicht den Anfang der Laufbahn, ' +
          'sondern wie die Wertung vom Startwert aus aufholt.', 'info') + '</div>';
      }
    } else if (ui.x === 'season') {
      html = p + 'Waagrecht: die wievielte Saison seit dem ersten erfassten Fest (nach Kalenderjahren gezählt). Punkt = Wertung am Saisonende, wie in der Tabelle «Saisons» (antippen für Details); ' +
        'hohler Punkt = Saison ohne Platz in der Saisonrangliste (zu wenige Gänge oder Saison ohne Rangierung). ' +
        'Gestrichelt: Saisons ohne erfasstes Fest dazwischen – sie zählen als Jahr der Laufbahn, haben aber keinen Wert. ' +
        'Der Verlauf innerhalb einer Saison fehlt in dieser Ansicht. ' + eras(shown) + ' Unten lässt sich der Bereich eingrenzen.</p>';
      if (early.length) {
        html += '<div class="mt-2">' + SE.note('<strong>«1. Saison» ist die erste erfasste Saison, nicht zwingend die erste der Laufbahn.</strong> ' + begin +
          'Wahrscheinlich ' + (one ? 'hat er' : 'haben sie') + ' davor schon Saisons bestritten, die nicht erfasst sind – sicher ist das nicht.', 'info') + '</div>';
      }
    } else {
      html = p + 'Waagrecht: Alter als Kalenderjahr minus Jahrgang. Bekannt ist nur der Jahrgang, nicht der Geburtstag: der Abschnitt von 22 bis 23 ist das Kalenderjahr, ' +
        'in dem ein Schwinger 22 wird – sein wirkliches Alter liegt bis zu einem Jahr darunter, und zwei Schwinger an derselben Stelle können fast ein Jahr auseinanderliegen. ' +
        'Punkt = Wertung nach einem Fest (antippen für Details). Gestrichelt: Rückführung Richtung 1500 am Saisonwechsel (1. April), gehört zu keinem Fest. ' +
        'Gepunktet: Zeit ohne Kampf bis zum Datenstand. ' + eras(shown) + ' Unten lässt sich der Bereich eingrenzen.</p>';
      if (early.length) {
        /* the age the curve starts at: year of the first history row minus the birth year */
        html += '<div class="mt-2">' + SE.note('<strong>Die Kurve beginnt nicht zwingend am Anfang der Laufbahn.</strong> ' +
          (firstSeason !== null ? 'Die Daten beginnen ' + SE.esc(firstSeason) + ' und sind in den ersten Jahren lückenhaft. ' : '') +
          early.map(function (e) {
            return SE.esc(plainName(e)) + ' (ab ' + SE.esc(startYear(e) - e.h.by) + ', erstes erfasstes Fest ' + SE.esc(startYear(e)) + ')';
          }).join(', ') + ': Die Kurve beginnt im Jahr des ersten erfassten Fests und beim Startwert 1500. ' +
          'Wahrscheinlich ' + (one ? 'schwang er' : 'schwangen sie') + ' schon in jüngeren Jahren, die nicht erfasst sind – sicher ist das nicht.', 'info') + '</div>';
      }
    }
    if (metaState === 'missing') {
      /* the first sign of likelyTruncated() cannot be read: say so instead of staying silent */
      html += '<div class="mt-2">' + SE.note('Der Beginn der Daten konnte nicht gelesen werden. Wessen Laufbahn schon vor den erfassten Daten begann, ' +
        'ist deshalb hier nur angegeben, wo es sich aus dem Jahrgang ergibt; bei den übrigen kann der erste erfasste Gang ebenfalls nicht der erste sein. ' +
        'Bitte die Seite neu laden.') + '</div>';
    }
    return html;
  }

  function chartSection(list) {
    var mode = ui.x, shown = drawable(list);
    var both = mode === 'time' ? overlap(list) : null, shared = sharedRange(shown);
    var html = '<h2 class="mt-8 text-lg font-bold">Verlauf der Wertung</h2>' +
      '<div class="mt-2 flex flex-wrap items-center gap-2 text-sm" role="group" aria-label="Legende; antippen hebt einen Schwinger hervor">';
    shown.forEach(function (e) {
      var on = ui.focus === e.id;
      html += '<button type="button" class="se-chip flex items-center gap-1' + (on ? ' se-row-on font-semibold' : '') + '" data-focus="' + SE.esc(e.id) +
        '" aria-pressed="' + on + '" title="Im Diagramm hervorheben">' + swatch(e) + '<span>' + nameHtml(e) + '</span></button>';
    });
    html += '</div>';
    if (shown.length > 3) {
      /* many long careers overlap on a phone: say that one can be picked out */
      html += '<p class="mt-1 text-xs text-stone-500 dark:text-stone-400">Tipp: Einen Namen antippen hebt diese Laufbahn im Diagramm hervor, nochmals antippen zeigt wieder alle gleich.</p>';
    }
    html += axisSwitch();
    var first = firstBouts(shown);
    if (shared || first) {
      html += '<div class="mt-2 flex flex-wrap items-center gap-2 text-xs" role="group" aria-label="' + (shared ? shared.aria : 'Bereich der Gänge') + '">' +
        '<button type="button" class="se-chip' + (ui.zoom === 'all' ? ' se-chip-on' : '') + '" data-zoom="all" aria-pressed="' + (ui.zoom === 'all') + '">' + (shared ? shared.all : 'Alle Gänge') + '</button>' +
        (shared ? '<button type="button" class="se-chip' + (ui.zoom === 'common' ? ' se-chip-on' : '') + '" data-zoom="common" aria-pressed="' + (ui.zoom === 'common') +
          '">' + shared.label + '</button>' : '') +
        (first ? '<button type="button" class="se-chip' + (ui.zoom === 'first' ? ' se-chip-on' : '') + '" data-zoom="first" aria-pressed="' + (ui.zoom === 'first') +
          '">Erste ' + first + ' Gänge</button>' : '') + '</div>';
    }
    if (list.length > 1 && both) {
      html += '<div class="mt-2 flex flex-wrap items-center gap-2 text-xs" role="group" aria-label="Zeitraum">' +
        '<button type="button" class="se-chip' + (ui.zoom === 'all' ? ' se-chip-on' : '') + '" data-zoom="all" aria-pressed="' + (ui.zoom === 'all') + '">Ganze Zeit</button>' +
        '<button type="button" class="se-chip' + (ui.zoom === 'common' ? ' se-chip-on' : '') + '" data-zoom="common" aria-pressed="' + (ui.zoom === 'common') +
        '">Gemeinsame Zeit ' + SE.esc(both[0].slice(0, 4)) + '–' + SE.esc(both[1].slice(0, 4)) + '</button></div>';
    }
    /* age axis: who cannot be drawn is named, one note per athlete */
    list.forEach(function (e) {
      if (shown.indexOf(e) === -1) {
        html += '<div class="mt-2">' + SE.note('<strong>Jahrgang unbekannt:</strong> ' + SE.esc(plainName(e)) + ' fehlt in dieser Ansicht. ' +
          'Sein Jahrgang steht nicht in den Quellen, sein Alter lässt sich deshalb nicht bestimmen. In den Ansichten «Zeit», «Gänge» und «Karrieresaison» ist er dabei.') + '</div>';
      }
    });
    if (!shown.length) {
      return html + '<div class="mt-2">' + SE.note(list.length === 1 ? 'Ohne Jahrgang lässt sich nach Alter kein Diagramm zeichnen.'
        : 'Von keinem der ausgewählten Schwinger ist der Jahrgang bekannt: nach Alter lässt sich kein Diagramm zeichnen.', 'info') + '</div>';
    }
    var by = { time: '', bouts: ' nach Anzahl Gängen', age: ' nach Alter', season: ' nach Saison der Laufbahn' }[mode];
    html += '<div id="se-chart" class="mt-2 h-80 w-full sm:h-96" role="img" aria-label="Verlauf der ELO-Wertung von ' +
      SE.esc(shown.map(plainName).join(', ')) + by + '; die Werte stehen in den Tabellen darunter und in den Profilen"></div>';
    if (mode !== 'time') { return html + axisNotes(list, shown); }
    html += '<p class="mt-1 text-xs text-stone-500 dark:text-stone-400">Punkt = Wertung nach einem Fest (antippen für Details); jeder Schwinger hat eine eigene Farbe und Punktform. ' +
      'Gestrichelt: Rückführung Richtung 1500 am Saisonwechsel (1. April), gehört zu keinem Fest. Gepunktet: Zeit ohne Kampf bis zum Datenstand. ' +
      'Unten lässt sich der Zeitraum eingrenzen.</p>';
    return html;
  }

  function drawChart(list) {
    var el = SE.$('se-chart');
    if (chart) { chart.dispose(); chart = null; }
    if (!el) { return; }
    try {
      if (!window.echarts) { throw new Error('ECharts nicht geladen'); }
      chart = window.echarts.init(el, null, { renderer: 'canvas' });
      chart.setOption(chartOption(drawable(list)), true);
      /* the single Gänge come and go with the visible range (slider) */
      var mine = chart;
      chart.on('datazoom', function () {
        if (!gangPoints.length || mine !== chart) { return; }
        var z = mine.getOption().dataZoom[0];
        var show = (z.endValue - z.startValue) <= dotLimit();
        if (show === gangDots) { return; }
        gangDots = show;
        mine.setOption({ series: gangPoints.map(function (g) { return { id: g.id, data: show ? g.data : [] }; }) });
      });
    } catch (err) {
      chart = null;
      el.innerHTML = SE.note('Das Diagramm konnte nicht gezeichnet werden; die Werte stehen in den Tabellen und in den Profilen.');
      el.className = 'mt-2';
    }
  }

  // ------------------------------------------------------------------ seasons
  function seasons(list) {
    var by = [], years = {};
    list.forEach(function (e, i) {
      by[i] = {};
      SE.table(e.h.seasons).forEach(function (s) { by[i][s.season] = s; years[s.season] = true; });
    });
    var order = Object.keys(years).map(Number).sort(function (a, b) { return b - a; });
    if (!order.length) { return ''; }
    var html = '<h2 class="mt-8 text-lg font-bold">Saisons</h2>' +
      '<div class="mt-2 overflow-x-auto"><table class="se-table"><thead><tr><th class="se-th">Saison</th>';
    list.forEach(function (e) { html += head(e); });
    html += '</tr></thead><tbody>';
    order.forEach(function (y) {
      html += '<tr><td class="se-td tabular-nums"><a class="se-link" href="index.html#saison-' + SE.esc(y) + '">' + SE.esc(y) + '</a></td>';
      list.forEach(function (e, i) {
        var s = by[i][y];
        html += '<td class="se-td whitespace-nowrap tabular-nums">' + (s ? '<span class="font-semibold">' + SE.rating(s.rating) + '</span>' +
          muted(s.pos === null ? 'ohne Platz' : 'Platz ' + SE.num(s.pos)) : '') + '</td>';
      });
      html += '</tr>';
    });
    return html + '</tbody></table></div>' +
      '<p class="mt-2 text-xs text-stone-500 dark:text-stone-400">Wertung am Saisonende und Platz in der Saisonrangliste. «ohne Platz»: zu wenige Gänge in der Saison ' +
      'oder insgesamt, oder eine Saison ohne Rangierung (2011, 2020). Leeres Feld: kein erfasstes Fest in dieser Saison.</p>';
  }

  // ------------------------------------------------------------------ direct bouts
  /* The bouts between a and b, each once, from a's side - read from a's file (b's file
   * holds the same bouts mirrored and is only used when a's could not be loaded). */
  function duels(a, b) {
    var src = a.bouts ? a : b, other = a.bouts ? b : a, flip = !a.bouts;
    if (!src.bouts) { return null; }
    var oi = src.bouts.opps.indexOf(other.id), out = [];
    if (oi === -1) { return out; }
    var fest = {};
    src.rows.forEach(function (r) { fest[r.fest_id] = { name: r.fest, date: r.date, cat: r.cat }; });
    SE.table(src.bouts.other).forEach(function (o) { fest[o.id] = o; });
    src.bouts.fests.forEach(function (f) {
      f[1].forEach(function (r) {
        /* row = [gang, opp, res, g, go, flags, d] (BOUT_SIDE_COLS of the exporter) */
        if (r[1] !== oi) { return; }
        var res = r[2], info = fest[f[0]] || { name: 'Fest', date: null, cat: null };
        out.push({
          fest_id: f[0], fest: info.name, date: info.date, cat: info.cat, gang: r[0],
          res: flip && res ? 3 - res : res, ga: flip ? r[4] : r[3], gb: flip ? r[3] : r[4], flags: r[5]
        });
      });
    });
    out.sort(function (x, y) { return x.date < y.date ? 1 : x.date > y.date ? -1 : y.gang - x.gang; });
    return out;
  }

  /* A missing grade is said to be missing (extra bouts, one-sided sheets), never a dash
   * that could be read as zero. */
  function gradeText(g) {
    return g === null || g === undefined ? 'ohne Note' : SE.grade(g);
  }

  function duelsHtml(a, b, list) {
    var html = '<div class="overflow-x-auto"><table class="se-table"><thead><tr><th class="se-th">Fest</th><th class="se-th">Ausgang</th>' +
      '<th class="se-th text-right" title="Note ' + SE.esc(plainName(a)) + ' : Note ' + SE.esc(plainName(b)) + '">Noten</th></tr></thead><tbody>';
    list.forEach(function (d) {
      var tags = '';
      if (d.flags & B_SCHLUSSGANG) { tags += ' <span class="se-badge se-badge-warn">Schlussgang</span>'; }
      if ((d.flags & B_EXTRA) && !(d.flags & B_SCHLUSSGANG)) { tags += ' <span class="se-badge se-badge-muted" title="Zusatzgang bei ungerader Teilnehmerzahl">Zusatzgang</span>'; }
      if (d.flags & B_GANG_UNCERTAIN) { tags += ' <span class="se-badge se-badge-muted" title="Die Nummer des Gangs ist aus der Quelle nicht eindeutig">Gang unsicher</span>'; }
      if (d.flags & B_UNRATED) { tags += ' <span class="se-badge se-badge-muted" title="Mannschaftsanlass oder Fest im Ausland: zählt nicht für die Wertung">nicht gewertet</span>'; }
      var winner = d.res === 1 ? a : d.res === 2 ? b : null;
      html += '<tr><td class="se-td"><a class="se-link" href="' + SE.festUrl(d.fest_id, a.id) + '">' + SE.esc(d.fest) + '</a>' +
        muted('<span class="tabular-nums">' + SE.esc(SE.date(d.date)) + '</span>' + (d.cat ? ' · ' + SE.esc(SE.category(d.cat)) : '') +
          ' · ' + SE.esc(d.gang) + '. Gang' + tags) + '</td>' +
        '<td class="se-td">' + (winner ? '<span class="flex items-baseline gap-1">' + swatch(winner) + '<span>Sieg ' + nameHtml(winner) + '</span></span>'
          : '<span class="text-stone-600 dark:text-stone-300">gestellt</span>') + '</td>' +
        '<td class="se-td whitespace-nowrap text-right tabular-nums">' + gradeText(d.ga) + ' : ' + gradeText(d.gb) + '</td></tr>';
    });
    return html + '</tbody></table></div>';
  }

  function renderDuels() {
    var box = SE.$('se-duels'), list = ok();
    if (!box) { return; }
    if (list.length < 2) { box.innerHTML = ''; return; }
    var html = '<h2 class="mt-8 text-lg font-bold">Direkte Gänge</h2>', pairs = 0, n = list.length * (list.length - 1) / 2;
    var unrated = 0;
    var failed = list.filter(function (e) { return e.boutsError; });
    html += '<div class="mt-2 space-y-3">';
    for (var i = 0; i < list.length; i++) {
      for (var j = i + 1; j < list.length; j++) {
        var a = list[i], b = list[j], ds = duels(a, b);
        pairs++;
        var title = '<span class="font-medium">' + nameHtml(a) + ' – ' + nameHtml(b) + '</span>';
        if (ds === null) {
          html += '<div class="se-card text-sm">' + title + (a.boutsError && b.boutsError
            ? muted('Die Gänge konnten nicht geladen werden. Bitte Seite neu laden.') : muted('Wird geladen …')) + '</div>';
          continue;
        }
        if (!ds.length) {
          html += '<div class="se-card text-sm">' + title + muted('Kein direkter Gang erfasst.') + '</div>';
          continue;
        }
        var w = 0, d = 0, l = 0;
        ds.forEach(function (x) {
          if (x.res === 1) { w++; } else if (x.res === 2) { l++; } else { d++; }
          if (x.flags & B_UNRATED) { unrated++; }
        });
        /* each bout once: a win of one is the other's defeat */
        var tally = '<span class="mt-1 flex flex-wrap items-center gap-x-2 text-sm">' +
          '<span class="flex items-center gap-1">' + swatch(a) + '<span class="font-bold tabular-nums">' + w + '</span> ' + (w === 1 ? 'Sieg' : 'Siege') + '</span>' +
          '<span><span class="font-bold tabular-nums">' + d + '</span> gestellt</span>' +
          '<span class="flex items-center gap-1">' + swatch(b) + '<span class="font-bold tabular-nums">' + l + '</span> ' + (l === 1 ? 'Sieg' : 'Siege') + '</span>' +
          '<span class="text-xs text-stone-500 dark:text-stone-400">' + SE.num(ds.length) + (ds.length === 1 ? ' Gang' : ' Gänge') + '</span></span>';
        html += '<details class="se-details"' + (n <= 3 ? ' open' : '') + '><summary>' + title + '</summary>' + tally +
          '<div class="mt-2">' + duelsHtml(a, b, ds) + '</div></details>';
      }
    }
    html += '</div><p class="mt-2 text-xs text-stone-500 dark:text-stone-400">Alle erfassten Gänge der beiden gegeneinander seit 2011, jeder Gang einmal gezählt; Noten in der Reihenfolge der Namen. ' +
      '«ohne Note»: in der Quelle steht keine Note. Der Schlussgang ist nur markiert, wo ihn die Quelle ausweist; bei etwa 2 % der Gänge ist die Nummer unsicher. ' +
      'Feste mit lückenhaften Listen können Gänge vermissen lassen.' +
      /* visible text, not a tooltip (touch): why the tally can exceed the rated festivals below */
      (unrated ? ' <span class="font-medium">«nicht gewertet»</span>: ' + (unrated === 1 ? 'Ein Gang stammt' : SE.num(unrated) + ' Gänge stammen') +
        ' von einem Mannschaftsanlass oder einem Fest im Ausland. Solche Gänge zählen nicht für die Wertung, sind in der Bilanz hier aber mitgezählt; ' +
        'unter «Gemeinsame Feste» fehlen diese Anlässe, weil dort nur gewertete Feste stehen.' : '') + '</p>';
    if (failed.length && failed.length < list.length) {
      html += '<div class="mt-2">' + SE.note('Ein Teil der Gänge konnte nicht geladen werden; die Listen sind trotzdem vollständig, solange von jedem Paar eine Seite vorliegt.', 'info') + '</div>';
    }
    box.innerHTML = html;
  }

  // ------------------------------------------------------------------ common festivals
  function renderCommon() {
    var box = SE.$('se-common'), list = ok();
    if (!box) { return; }
    if (list.length < 2) { box.innerHTML = ''; return; }
    var fests = {};
    list.forEach(function (e, i) {
      e.rows.forEach(function (r) {
        var f = fests[r.fest_id] || (fests[r.fest_id] = { id: r.fest_id, name: r.fest, date: r.date, cat: r.cat, who: {}, n: 0 });
        f.who[i] = r;
        f.n++;
      });
    });
    /* festivals with a direct bout between two of the selected athletes */
    var met = {};
    for (var i = 0; i < list.length; i++) {
      for (var j = i + 1; j < list.length; j++) {
        (duels(list[i], list[j]) || []).forEach(function (d) { met[d.fest_id] = true; });
      }
    }
    var all = Object.keys(fests).map(function (k) { return fests[k]; });
    var two = all.filter(function (f) { return f.n >= 2; });
    var every = all.filter(function (f) { return f.n === list.length; });
    var mode = list.length > 2 ? ui.common : 'all';
    var rows = (mode === 'all' ? every : two).sort(function (x, y) {
      return x.date < y.date ? 1 : x.date > y.date ? -1 : y.id - x.id;
    });
    var html = '<h2 class="mt-8 text-lg font-bold">Gemeinsame Feste</h2>';
    if (list.length > 2) {
      html += '<div class="mt-2 flex flex-wrap items-center gap-2 text-xs" role="group" aria-label="Auswahl der Feste">' +
        '<button type="button" class="se-chip' + (mode === 'all' ? ' se-chip-on' : '') + '" data-common="all" aria-pressed="' + (mode === 'all') + '">Alle ' + list.length + ' dabei (' + SE.num(every.length) + ')</button>' +
        '<button type="button" class="se-chip' + (mode === 'two' ? ' se-chip-on' : '') + '" data-common="two" aria-pressed="' + (mode === 'two') + '">Mindestens zwei dabei (' + SE.num(two.length) + ')</button></div>';
    }
    if (!rows.length) {
      html += '<p class="mt-2 text-sm">' + (list.length > 2 && mode === 'all' ? 'Kein gewertetes Fest, an dem alle ' + list.length + ' angetreten sind.'
        : 'Kein gewertetes Fest, an dem beide angetreten sind.') + '</p>';
      box.innerHTML = html;
      return;
    }
    html += '<p class="mt-2 text-sm text-stone-600 dark:text-stone-300">' + SE.num(rows.length) + (rows.length === 1 ? ' gewertetes Fest' : ' gewertete Feste') +
      (rows.length > ui.commonShown ? ', die neusten ' + ui.commonShown + ' angezeigt' : '') + '.</p>' +
      '<div class="mt-2 overflow-x-auto"><table class="se-table"><thead><tr><th class="se-th">Fest</th>';
    list.forEach(function (e) { html += head(e); });
    html += '</tr></thead><tbody>';
    rows.slice(0, ui.commonShown).forEach(function (f) {
      html += '<tr><td class="se-td"><a class="se-link" href="' + SE.festUrl(f.id) + '">' + SE.esc(f.name) + '</a>' +
        muted('<span class="tabular-nums">' + SE.esc(SE.date(f.date)) + '</span> · ' + SE.esc(SE.category(f.cat)) +
          (met[f.id] ? ' <span class="se-badge se-badge-muted">direkter Gang</span>' : '')) + '</td>';
      list.forEach(function (e, i) {
        var r = f.who[i];
        if (!r) { html += '<td class="se-td"></td>'; return; }
        var diff = r.after - r.before;
        html += '<td class="se-td whitespace-nowrap tabular-nums">' + SE.num(r.score, 1) + ' / ' + SE.esc(r.n) +
          '<span class="block text-xs"><span class="text-stone-500 dark:text-stone-400">' + SE.rating(r.after) + '</span> <span class="' +
          (diff >= 0.5 ? 'se-up' : diff <= -0.5 ? 'se-down' : '') + '">' + SE.signed(diff) + '</span></span></td>';
      });
      html += '</tr>';
    });
    html += '</tbody></table></div>';
    if (rows.length > ui.commonShown) {
      html += '<div class="mt-4 text-center"><button type="button" class="se-btn" data-more="1">Weitere ' +
        Math.min(PAGE, rows.length - ui.commonShown) + ' anzeigen (' + SE.num(rows.length - ui.commonShown) + ' übrig)</button></div>';
    }
    html += '<p class="mt-2 text-xs text-stone-500 dark:text-stone-400">Punkte aus Gängen (Sieg 1, Gestellter ½), darunter die Wertung nach dem Fest und ihre Veränderung an diesem Fest. ' +
      'Nur gewertete Feste; Mannschaftsanlässe und Feste im Ausland fehlen hier.</p>';
    box.innerHTML = html;
  }

  // ------------------------------------------------------------------ page
  function invite() {
    return '<div class="se-card"><p class="font-medium">Noch niemand ausgewählt.</p>' +
      '<p class="mt-1 text-sm text-stone-600 dark:text-stone-300">Oben im Feld «Schwinger hinzufügen» einen Namen eintippen und antippen – zwei bis ' + MAX +
      ' Schwinger lassen sich vergleichen. Die Auswahl steht in der Adresse der Seite; der Link lässt sich so weitergeben.</p>' +
      '<p class="mt-3"><button type="button" class="se-btn" id="se-example">Beispiel: die drei Besten der aktuellen Rangliste</button></p></div>';
  }

  function render() {
    renderPicked();
    renderSlots();
    /* while an added athlete loads, the comparison shown so far stays in place */
    if (entries.some(function (e) { return e.status === 'loading'; })) { return; }
    var list = ok();
    document.title = (list.length ? list.map(function (e) { return e.h.name; }).join(' – ') + ' – ' : '') + 'Vergleich – Schwinger-ELO';
    if (!list.length) {
      if (chart) { chart.dispose(); chart = null; }
      view.innerHTML = invite();
      return;
    }
    var html = '';
    if (list.length === 1) {
      html += '<div class="mb-4">' + SE.note('Erst ein Schwinger ausgewählt. Oben einen zweiten hinzufügen, um zu vergleichen: ' +
        'Verlauf im selben Diagramm, direkte Gänge und gemeinsame Feste.', 'info') + '</div>';
    }
    var notes = caveats(list);
    html += figures(list) + (notes ? '<div class="mt-4 space-y-3">' + notes + '</div>' : '') +
      chartSection(list) + seasons(list) + '<div id="se-duels"></div><div id="se-common"></div>';
    view.innerHTML = html;
    drawChart(list);
    renderDuels();
    renderCommon();
    loadBouts();
  }

  /* render() at the end of a download. Inside a promise an exception would vanish as an
   * unhandled rejection; it is thrown again outside, where the page's error banner
   * (app.js, window 'error') sees it. */
  function renderLoaded() {
    try {
      render();
    } catch (err) {
      window.setTimeout(function () { throw err; }, 0);
    }
  }

  view.addEventListener('click', function (ev) {
    var el = ev.target && ev.target.closest ? ev.target.closest('[data-focus],[data-zoom],[data-common],[data-more],[data-x],#se-example') : null;
    if (!el) { return; }
    if (el.hasAttribute('data-x')) {
      /* a link: with a modifier key the browser opens its address, else switch in place */
      if (ev.defaultPrevented || ev.button || ev.metaKey || ev.ctrlKey || ev.shiftKey) { return; }
      ev.preventDefault();
      var x = parseX(X_PARAM[el.getAttribute('data-x')]);   // only the known axes; else time
      if (x !== ui.x) {
        var top = window.scrollY;
        ui.x = x;
        ui.zoom = 'all';
        if (window.history && window.history.pushState) { window.history.pushState(null, '', compareUrl(ids())); }
        render();
        window.scrollTo(0, top);
      }
      return;
    }
    if (el.id === 'se-example') {
      el.disabled = true;
      SE.getJSON('data/rankings_latest.json').then(function (t) {
        var top = SE.table(t).slice(0, 3).map(function (r) { return r.id; }).filter(function (id) { return SE.ID_RE.test(id); });
        if (top.length) { setIds(top, true); } else { el.textContent = 'Noch keine Rangliste vorhanden'; }
      }).catch(function (err) { SE.showError(view, err); });
      return;
    }
    var y = window.scrollY;
    if (el.hasAttribute('data-focus')) {
      var id = el.getAttribute('data-focus');
      ui.focus = ui.focus === id ? null : id;
      render();
    } else if (el.hasAttribute('data-zoom')) {
      var zoomTo = el.getAttribute('data-zoom');
      ui.zoom = zoomTo === 'common' || zoomTo === 'first' ? zoomTo : 'all';
      render();
    } else if (el.hasAttribute('data-common')) {
      ui.common = el.getAttribute('data-common') === 'two' ? 'two' : 'all';
      ui.commonShown = PAGE;
      renderCommon();
    } else {
      ui.commonShown += PAGE;
      renderCommon();
    }
    window.scrollTo(0, y);
  });

  window.addEventListener('resize', function () { if (chart) { chart.resize(); } });
  window.addEventListener('popstate', function () {
    ui.x = parseX(SE.param('x'));
    setIds(parseIds(SE.param('ids')), false);
  });
  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    if (mq.addEventListener) { mq.addEventListener('change', render); }
  }

  /* The first season of the data, for the caveats of the axes (meta.json is loaded by
   * every page anyway). A failed download is a state of its own and is said on the page;
   * an error of the redraw is not a download error (renderLoaded). */
  SE.meta().then(function (meta) {
    firstSeason = meta && typeof meta.first_season === 'number' ? meta.first_season : null;
    metaState = firstSeason === null ? 'missing' : 'ok';
  }, function () {
    metaState = 'missing';
  }).then(function () {
    if (ui.x !== 'time') { renderLoaded(); }
  });

  ui.x = parseX(SE.param('x'));
  setIds(parseIds(SE.param('ids')), false);
})();
