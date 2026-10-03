/* Schwinger-ELO: festival list (fests.html) and one festival (fests.html?id=<fest_id>&a=<athlete_id>). */
(function () {
  'use strict';
  var SE = window.SE;
  var view = SE.$('se-view');
  var B_SCHLUSSGANG = 1, B_EXTRA = 2, B_NO_GRADE = 4, B_GANG_UNCERTAIN = 8;

  var STATUS_NOTE = {
    partial: 'Mit Lücken: Einzelne Zeilen der Resultatliste liessen sich nicht übernehmen (unleserliche Namen, fehlende Gegner, ' +
      'nicht ausgetragene Gänge). Es können Gänge oder Schwinger fehlen; gewertet wurde, was vorhanden ist.',
    unrated: 'Dieses Fest zählt nicht für die Wertung (Mannschaftsanlass oder Fest im Ausland). Die Gänge sind zur Information aufgeführt.'
  };
  var STATUS_LABEL = { unrated: 'nicht gewertet', none: 'keine Gänge erfasst' };

  // ------------------------------------------------------------------ list
  var listState = { year: null, cat: '', q: '' };

  function renderList(data) {
    var rows = SE.table(data);
    if (!rows.length) {
      view.innerHTML = '<h1 class="text-2xl font-bold tracking-tight">Feste</h1><p class="mt-3 text-sm">Noch keine Feste vorhanden.</p>';
      return;
    }
    var years = [];
    rows.forEach(function (r) {
      var y = r.date.slice(0, 4);
      if (years.indexOf(y) === -1) { years.push(y); }
    });
    if (listState.year === null) { listState.year = years[0]; }

    var html = '<h1 class="text-2xl font-bold tracking-tight">Feste</h1>' +
      '<p class="mt-1 text-sm text-stone-600 dark:text-stone-300">Was ist an einem Fest passiert? Teilnehmer, Gänge und Veränderung der Wertung.</p>' +
      '<div class="mt-4 flex flex-wrap gap-2">' +
      '<label class="sr-only" for="se-year">Jahr</label><select id="se-year" class="se-input"><option value="">Alle Jahre</option>';
    years.forEach(function (y) {
      html += '<option value="' + SE.esc(y) + '"' + (y === listState.year ? ' selected' : '') + '>' + SE.esc(y) + '</option>';
    });
    html += '</select><label class="sr-only" for="se-cat">Kategorie</label><select id="se-cat" class="se-input"><option value="">Alle Kategorien</option>';
    Object.keys(SE.CATEGORY).forEach(function (c) {
      html += '<option value="' + SE.esc(c) + '"' + (c === listState.cat ? ' selected' : '') + '>' + SE.esc(SE.CATEGORY[c]) + '</option>';
    });
    html += '</select><label class="sr-only" for="se-fq">Fest suchen</label>' +
      '<input id="se-fq" type="search" class="se-input w-full sm:w-auto sm:flex-1" placeholder="Fest oder Ort" value="' + SE.esc(listState.q) + '">' +
      '</div><div id="se-fests" class="mt-3"></div>';
    view.innerHTML = html;

    function draw() {
      var q = SE.norm(listState.q);
      var hits = rows.filter(function (r) {
        if (listState.year && r.date.slice(0, 4) !== listState.year) { return false; }
        if (listState.cat && r.cat !== listState.cat) { return false; }
        if (q && SE.norm(r.name + ' ' + (r.loc || '')).indexOf(q) === -1) { return false; }
        return true;
      });
      var shown = hits.slice(0, 300);
      var out = '<p class="text-sm text-stone-600 dark:text-stone-300">' + SE.num(hits.length) + ' Feste' +
        (hits.length > shown.length ? ', die ersten ' + shown.length + ' angezeigt – Auswahl einschränken' : '') + '</p>';
      if (shown.length) {
        out += '<ul class="mt-2">';
        shown.forEach(function (r) {
          var title = r.status === 'none'
            ? '<span class="font-medium">' + SE.esc(r.name) + '</span>'
            : '<a class="se-link font-medium" href="' + SE.festUrl(r.id) + '">' + SE.esc(r.name) + '</a>';
          out += '<li class="border-b border-stone-200 py-2 dark:border-stone-800">' + title +
            (STATUS_LABEL[r.status] ? ' <span class="se-badge se-badge-muted">' + STATUS_LABEL[r.status] + '</span>' : '') +
            '<span class="block text-xs text-stone-500 dark:text-stone-400"><span class="tabular-nums">' + SE.esc(SE.date(r.date)) + '</span> · ' +
            SE.esc(SE.category(r.cat, r.eidg)) +
            (r.bouts ? ' · ' + SE.num(r.athletes) + ' Schwinger, ' + SE.num(r.bouts) + ' Gänge' : '') +
            (r.status === 'partial' ? ' · mit Lücken' : '') + '</span></li>';
        });
        out += '</ul>';
      }
      SE.$('se-fests').innerHTML = out;
    }

    SE.$('se-year').addEventListener('change', function () { listState.year = this.value; draw(); });
    SE.$('se-cat').addEventListener('change', function () { listState.cat = this.value; draw(); });
    SE.$('se-fq').addEventListener('input', function () { listState.q = this.value; draw(); });
    draw();
  }

  // ------------------------------------------------------------------ one festival
  var RESULT = {
    win: { sym: '+', text: 'gewonnen', cls: 'se-up' },
    draw: { sym: '–', text: 'gestellt', cls: '' },
    loss: { sym: 'o', text: 'verloren', cls: 'se-down' }
  };

  /* Bouts from one athlete's point of view (the sheets list the better-ranked athlete
   * first; that order means nothing to the reader). */
  function boutsOf(f, idx) {
    var out = [];
    f.boutRows.forEach(function (b) {
      if (b.a !== idx && b.b !== idx) { return; }
      var mine = b.a === idx;
      var res = b.res === 0 ? 'draw' : ((b.res === 1) === mine ? 'win' : 'loss');
      out.push({ gang: b.gang, res: res, opp: f.athleteRows[mine ? b.b : b.a], grade: mine ? b.ga : b.gb, flags: b.flags });
    });
    out.sort(function (x, y) { return x.gang - y.gang; });
    return out;
  }

  function boutsHtml(f, idx) {
    var html = '<ol class="space-y-1">';
    boutsOf(f, idx).forEach(function (b) {
      var r = RESULT[b.res], tags = '';
      if (b.flags & B_SCHLUSSGANG) { tags += ' <span class="se-badge se-badge-warn">Schlussgang</span>'; }
      if ((b.flags & B_EXTRA) && !(b.flags & B_SCHLUSSGANG)) { tags += ' <span class="se-badge se-badge-muted" title="Zusatzgang bei ungerader Teilnehmerzahl">Zusatzgang</span>'; }
      if (b.flags & B_GANG_UNCERTAIN) { tags += ' <span class="se-badge se-badge-muted" title="Die Nummer des Gangs ist aus der Quelle nicht eindeutig">Gang unsicher</span>'; }
      html += '<li class="flex flex-wrap items-baseline gap-x-2"><span class="w-14 shrink-0 text-xs text-stone-500 dark:text-stone-400">' +
        SE.esc(b.gang) + '. Gang</span>' +
        '<span class="w-20 shrink-0 ' + r.cls + '"><span class="font-mono font-bold">' + r.sym + '</span> ' + r.text + '</span>' +
        '<span class="min-w-0 flex-1">' + SE.athleteLink(b.opp) +
        (b.opp.after !== null ? ' <span class="text-xs tabular-nums text-stone-500 dark:text-stone-400">(' + SE.rating(b.opp.before) + ')</span>' : '') + tags + '</span>' +
        '<span class="tabular-nums" title="Note">' + ((b.flags & (B_NO_GRADE | B_EXTRA)) && b.grade === null ? 'ohne Note' : SE.grade(b.grade)) + '</span></li>';
    });
    return html + '</ol>';
  }

  var SORTS = {
    pts: { label: 'Notenpunkte', key: function (a) { return a.pts === null ? -1 : a.pts; } },
    after: { label: 'Wertung nach dem Fest', key: function (a) { return a.after === null ? -1 : a.after; } },
    diff: { label: 'Veränderung', key: function (a) { return a.after === null ? -1e9 : a.after - a.before; } }
  };

  function renderFest(f, focusId) {
    document.title = f.name + ' – Schwinger-ELO';
    f.athleteRows = SE.table(f.athletes);
    f.athleteRows.forEach(function (a, i) { a.idx = i; });
    f.boutRows = SE.table(f.bouts);
    var rated = f.status !== 'unrated';
    var state = { sort: 'pts', open: {} };
    f.athleteRows.forEach(function (a) { if (focusId && a.id === focusId) { state.open[a.idx] = true; } });

    function draw() {
      var s = SORTS[state.sort];
      var order = f.athleteRows.slice().sort(function (x, y) { return s.key(y) - s.key(x) || x.idx - y.idx; });
      var html = '<p class="text-sm"><a class="se-link" href="fests.html">← Alle Feste</a></p>' +
        '<h1 class="mt-2 text-2xl font-bold tracking-tight">' + SE.esc(f.name) + '</h1>' +
        '<p class="mt-1 text-sm text-stone-600 dark:text-stone-300"><span class="tabular-nums">' + SE.esc(SE.date(f.date)) + '</span>' +
        (f.location ? ' · ' + SE.esc(f.location) : '') + ' · ' + SE.esc(SE.category(f.category, f.eidg_type)) + ' · ' +
        SE.num(f.athleteRows.length) + ' Schwinger, ' + SE.num(f.boutRows.length) + ' Gänge' +
        (f.url && /^https:\/\/www\.schlussgang\.ch\//.test(f.url) ? ' · <a class="se-link" href="' + SE.esc(f.url) + '" rel="noopener">Quelle: schlussgang.ch</a>' : '') + '</p>';
      if (STATUS_NOTE[f.status]) {
        html += '<div class="mt-3">' + SE.note(STATUS_NOTE[f.status], f.status === 'partial' ? 'info' : '') + '</div>';
      }
      html += '<div class="mt-4 flex flex-wrap items-center gap-2 text-sm"><label for="se-sort">Sortieren nach</label>' +
        '<select id="se-sort" class="se-input">';
      Object.keys(SORTS).forEach(function (k) {
        if (!rated && k !== 'pts') { return; }
        html += '<option value="' + k + '"' + (k === state.sort ? ' selected' : '') + '>' + SORTS[k].label + '</option>';
      });
      html += '</select></div>' +
        '<div class="mt-2 overflow-x-auto"><table class="se-table"><thead><tr><th class="se-th">Schwinger</th>' +
        '<th class="se-th text-right" title="gewonnen – gestellt – verloren">S–G–N</th>' +
        '<th class="se-th text-right hidden sm:table-cell">Noten</th>' +
        (rated ? '<th class="se-th text-right">Wertung</th><th class="se-th text-right">+/−</th>' : '') +
        '</tr></thead><tbody>';
      order.forEach(function (a) {
        var open = !!state.open[a.idx], focus = focusId && a.id === focusId;
        var diff = a.after === null ? null : a.after - a.before;
        html += '<tr' + (focus ? ' id="se-focus"' : '') + ' class="' + (focus ? 'se-row-on' : '') + '">' +
          '<td class="se-td">' + SE.athleteLink(a) +
          '<span class="block text-xs text-stone-500 dark:text-stone-400">' + SE.subline(a) + '</span>' +
          '<button type="button" class="se-link mt-0.5 text-xs" data-toggle="' + a.idx + '" aria-expanded="' + open + '">' +
          (open ? 'Gänge ausblenden' : 'Gänge zeigen') + '</button></td>' +
          '<td class="se-td whitespace-nowrap text-right tabular-nums">' + a.w + '–' + a.d + '–' + a.l + '</td>' +
          '<td class="se-td text-right tabular-nums hidden sm:table-cell">' + (a.pts ? SE.num(a.pts, 2) : '–') + '</td>' +
          (rated ? '<td class="se-td whitespace-nowrap text-right tabular-nums">' +
            (a.after === null ? '–' : '<span class="text-xs text-stone-500 dark:text-stone-400">' + SE.rating(a.before) + ' → </span><span class="font-semibold">' + SE.rating(a.after) + '</span>') + '</td>' +
            '<td class="se-td text-right tabular-nums ' + (diff > 0 ? 'se-up' : diff < 0 ? 'se-down' : '') + '">' + SE.signed(diff) + '</td>' : '') +
          '</tr>';
        if (open) {
          html += '<tr class="' + (focus ? 'se-row-on' : '') + '"><td class="se-td text-sm" colspan="' + (rated ? 5 : 3) + '">' + boutsHtml(f, a.idx) + '</td></tr>';
        }
      });
      html += '</tbody></table></div>' +
        '<p class="mt-3 text-xs text-stone-500 dark:text-stone-400">S–G–N: gewonnen – gestellt – verloren. Noten: Summe der gedruckten Noten, ' +
        'keine offizielle Rangliste (dafür die Quelle beachten). Wertung vor → nach dem Fest; in Klammern bei den Gängen die Wertung des Gegners vor dem Fest. ' +
        'Der Schlussgang ist nur markiert, wo ihn die Quelle ausweist.</p>';
      view.innerHTML = html;

      SE.$('se-sort').addEventListener('change', function () { state.sort = this.value; draw(); });
      var toggles = view.querySelectorAll('[data-toggle]');
      for (var i = 0; i < toggles.length; i++) {
        toggles[i].addEventListener('click', function () {
          var k = Number(this.getAttribute('data-toggle'));
          state.open[k] = !state.open[k];
          var y = window.scrollY;
          draw();
          window.scrollTo(0, y);
        });
      }
    }

    draw();
    var focus = SE.$('se-focus');
    if (focus && focus.scrollIntoView) { focus.scrollIntoView({ block: 'center' }); }
  }

  function festNotFound() {
    view.innerHTML = '<h1 class="text-2xl font-bold tracking-tight">Fest nicht gefunden</h1>' +
      '<p class="mt-2 text-sm">Zu diesem Link gibt es kein Fest mit erfassten Gängen. <a class="se-link" href="fests.html">Zur Liste aller Feste</a></p>';
  }

  var id = SE.param('id');
  if (id === null) {
    SE.getJSON('data/festivals.json').then(renderList).catch(function (err) { SE.showError(view, err); });
  } else if (!/^\d{1,9}$/.test(id)) {
    festNotFound();
  } else {
    var focusId = SE.param('a');
    if (focusId && !SE.ID_RE.test(focusId)) { focusId = null; }
    SE.getJSON('data/fests/fest_' + id + '.json').then(function (f) { renderFest(f, focusId); }).catch(function (err) {
      if (err.status === 404) { festNotFound(); } else { SE.showError(view, err); }
    });
  }
})();
