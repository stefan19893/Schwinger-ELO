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
   * first; that order means nothing to the reader). `d` is what the bout contributed to
   * this athlete's rating: the file gives it for athlete a, for b it is the negative. It
   * is null where the file gives none - a festival that does not count, or a bout with an
   * athlete who is not published by name. */
  function boutsOf(f, idx) {
    var out = [];
    f.boutRows.forEach(function (b) {
      if (b.a !== idx && b.b !== idx) { return; }
      var mine = b.a === idx;
      var res = b.res === 0 ? 'draw' : ((b.res === 1) === mine ? 'win' : 'loss');
      var d = (b.d === null || b.d === undefined) ? null : (mine ? b.d : -b.d);
      out.push({ gang: b.gang, res: res, opp: f.athleteRows[mine ? b.b : b.a], grade: mine ? b.ga : b.gb, flags: b.flags, d: d });
    });
    out.sort(function (x, y) { return x.gang - y.gang; });
    return out;
  }

  function upDown(n) { return n > 0.04 ? 'se-up' : n < -0.04 ? 'se-down' : ''; }

  var MUTED = 'text-xs text-stone-500 dark:text-stone-400';

  /* The Gänge of a listed athlete against athletes who are not published by name carry no
   * contribution in their row. Below the Gänge the page gives what they contributed
   * together (festival change minus the listed contributions) - with a single such Gang
   * that number is this Gang's contribution, and the page says so. `true` hides that
   * number, and the sum of the listed contributions with it, for one and for several
   * Gänge alike; the page then only says how many such Gänge there are. (The about page
   * describes the number: about.html "Pro Fest, nicht pro Gang" and about.js se-minors3
   * - adapt them when this is switched.) */
  var HIDE_REMAINDER = false;

  /* The opened Gänge of one athlete. With `f.perGang` (a festival that counts, a file
   * that carries the contributions) a listed athlete's rows show what each Gang
   * contributed, and below them how that adds up to the festival's change. A withheld
   * athlete's own rows never show a number, and a Gang against a withheld athlete has
   * none of its own: those are given as one remainder (festival change minus the listed
   * contributions), as on the comparison page. */
  function boutsHtml(f, a) {
    var list = boutsOf(f, a.idx);
    var numbers = f.perGang && !!a.id && !a.anon && a.after !== null;
    /* the symbols are explained in every opened block (on a phone the words are left out) */
    var html = '<p class="' + MUTED + '"><span class="font-mono font-bold">+</span> gewonnen · <span class="font-mono font-bold">–</span> gestellt · ' +
      '<span class="font-mono font-bold">o</span> verloren' +
      (numbers ? ' · in Klammern die Wertung des Gegners vor dem Fest · ' +
        'mit Vorzeichen der <strong>Beitrag des Gangs zur Wertung</strong> · rechts die Note' : ' · rechts die Note') + '</p>';
    html += '<ol class="mt-1 space-y-1">';
    var sum = 0, hidden = 0;
    list.forEach(function (b) {
      var r = RESULT[b.res], tags = '';
      if (b.flags & B_SCHLUSSGANG) { tags += ' <span class="se-badge se-badge-warn">Schlussgang</span>'; }
      if ((b.flags & B_EXTRA) && !(b.flags & B_SCHLUSSGANG)) { tags += ' <span class="se-badge se-badge-muted" title="Zusatzgang bei ungerader Teilnehmerzahl">Zusatzgang</span>'; }
      if (b.flags & B_GANG_UNCERTAIN) { tags += ' <span class="se-badge se-badge-muted" title="Die Nummer des Gangs ist aus der Quelle nicht eindeutig">Gang unsicher</span>'; }
      var contribution = '';
      if (numbers) {
        if (b.d === null) {
          hidden += 1;
          contribution = '<span class="w-14 shrink-0 text-right text-stone-500 dark:text-stone-400" title="Kein Beitrag in dieser Zeile: Der Gegner wird nicht mit Namen veröffentlicht">–</span>';
        } else {
          sum += b.d;
          contribution = '<span class="w-14 shrink-0 text-right font-semibold tabular-nums ' + upDown(b.d) + '" title="Beitrag dieses Gangs zur Wertung">' + SE.signed1(b.d) + '</span>';
        }
      }
      html += '<li class="flex items-baseline gap-x-2"><span class="shrink-0 tabular-nums ' + MUTED + '" title="Gang">' + SE.esc(b.gang) + '.</span>' +
        '<span class="shrink-0 ' + r.cls + '" title="' + r.text + '"><span class="font-mono font-bold">' + r.sym + '</span><span class="hidden sm:inline"> ' + r.text + '</span></span>' +
        '<span class="min-w-0 flex-1">' + SE.athleteLink(b.opp) +
        (b.opp.after !== null ? ' <span class="text-xs tabular-nums text-stone-500 dark:text-stone-400">(' + SE.rating(b.opp.before) + ')</span>' : '') + tags + '</span>' +
        contribution +
        '<span class="w-10 shrink-0 text-right tabular-nums' + (numbers ? ' ' + MUTED : '') + '" title="Note">' +
        ((b.flags & (B_NO_GRADE | B_EXTRA)) && b.grade === null ? 'ohne Note' : SE.grade(b.grade)) + '</span></li>';
    });
    html += '</ol>';
    if (numbers) {
      var diff = a.after - a.before, rest = diff - sum;
      html += '<p class="mt-2 text-xs">Veränderung am Fest: <strong class="tabular-nums ' + upDown(diff) + '">' + SE.signed1(diff) + '</strong>';
      if (hidden && HIDE_REMAINDER) {
        html += '. Darin ' + SE.hiddenGaenge(hidden) + '; ' + (hidden === 1 ? 'sein Beitrag wird' : 'ihre Beiträge werden') + ' hier nicht gezeigt.';
      } else if (hidden) {
        /* true for one and for several: one hidden Gang - the number is its contribution */
        html += ' = Beiträge oben <span class="tabular-nums">' + SE.signed1(sum) + '</span> und ' + SE.hiddenGaenge(hidden) +
          ' <span class="tabular-nums">' + SE.signed1(rest) + '</span>' +
          (hidden === 1 ? ' – der Beitrag dieses einen Gangs.' : ' – ihre Summe; einzeln werden sie nicht gezeigt.');
      } else {
        html += Math.abs(rest) >= 0.05 ? ' (die Beiträge sind gerundet und ergeben ' + SE.signed1(sum) + ').' : ' – die Summe der Beiträge.';
      }
      if (a.exp !== null && a.exp !== undefined) {
        html += ' Geholt: ' + SE.num(a.w + a.d / 2, 1) + ' von ' + SE.num(a.w + a.d + a.l) + ' Punkten, erwartet waren ' + SE.num(a.exp, 1) +
          ' (Sieg 1, Gestellter ½).';
      }
      html += '</p><p class="mt-1 ' + MUTED + '">Die Wertung wird pro Fest berechnet, gegen die Wertungen vor dem Fest; ' +
        'die Beiträge sind die Aufteilung dieser einen Änderung auf die Gänge, keine Wertung nach jedem Gang.</p>';
    } else if (f.perGang && a.anon) {
      html += '<p class="mt-2 ' + MUTED + '">Ohne Zahlen zur Wertung: Für Schwinger, die nicht mit Namen veröffentlicht werden, wird keine Wertung gezeigt.</p>';
    }
    return html;
  }

  var SORTS = {
    pts: { label: 'Notenpunkte', key: function (a) { return a.pts === null ? -1 : a.pts; } },
    diff: { label: 'Veränderung der Wertung', key: function (a) { return a.after === null ? -1e9 : a.after - a.before; } },
    before: { label: 'Wertung vor dem Fest', key: function (a) { return a.before === null ? -1 : a.before; } },
    after: { label: 'Wertung nach dem Fest', key: function (a) { return a.after === null ? -1 : a.after; } }
  };

  var UPSET_MIN_GAP = 50;      // a win against less than this many points more is no surprise
  var MOVERS = 3;

  function who(a) {
    return SE.athleteLink(a);
  }

  /* "Das Fest in Zahlen": strength of the field, largest gains and losses, biggest upset.
   * Everything is computed over the athletes listed with a rating: a withheld athlete has
   * no rating value in the file, so he is in none of these figures, and the box says so. */
  function summaryHtml(f) {
    var listed = f.athleteRows.filter(function (a) { return a.after !== null && a.before !== null; });
    if (listed.length < 2) { return ''; }
    var anon = f.athleteRows.filter(function (a) { return a.anon; }).length;
    var mean = 0, top = listed[0], low = listed[0];
    listed.forEach(function (a) {
      mean += a.before;
      if (a.before > top.before) { top = a; }
      if (a.before < low.before) { low = a; }
    });
    mean /= listed.length;
    var lines = [];
    lines.push('<strong>Feld:</strong> ' + SE.num(listed.length) + ' Schwinger mit Wertung, vor dem Fest im Mittel <span class="tabular-nums">' + SE.rating(mean) + '</span>' +
      (top.before - low.before >= 1 ? '; die höchste hatte ' + who(top) + ' (<span class="tabular-nums">' + SE.rating(top.before) + '</span>)' : ' – alle beim Startwert') + '.');
    var byDiff = listed.slice().sort(function (x, y) { return (y.after - y.before) - (x.after - x.before) || x.idx - y.idx; });
    function movers(part) {
      return part.map(function (a) {
        var d = a.after - a.before;
        return who(a) + ' <span class="tabular-nums ' + upDown(d) + '">' + SE.signed(d) + '</span>';
      }).join(', ');
    }
    var up = byDiff.slice(0, MOVERS).filter(function (a) { return a.after - a.before > 0.5; });
    var down = byDiff.slice(-MOVERS).reverse().filter(function (a) { return a.after - a.before < -0.5; });
    if (up.length) { lines.push('<strong>Am meisten gewonnen:</strong> ' + movers(up) + '.'); }
    if (down.length) { lines.push('<strong>Am meisten verloren:</strong> ' + movers(down) + '.'); }
    /* the win against the largest rating lead, among the bouts of two listed athletes */
    var upset = null;
    f.boutRows.forEach(function (b) {
      if (b.res === 0) { return; }
      var w = f.athleteRows[b.res === 1 ? b.a : b.b], l = f.athleteRows[b.res === 1 ? b.b : b.a];
      if (w.before === null || l.before === null || w.after === null || l.after === null) { return; }
      var gap = l.before - w.before;
      if (gap >= UPSET_MIN_GAP && (!upset || gap > upset.gap)) { upset = { w: w, l: l, gap: gap, gang: b.gang }; }
    });
    if (upset) {
      lines.push('<strong>Grösste Überraschung:</strong> ' + who(upset.w) + ' (<span class="tabular-nums">' + SE.rating(upset.w.before) + '</span>) gewinnt im ' +
        SE.esc(upset.gang) + '. Gang gegen ' + who(upset.l) + ' (<span class="tabular-nums">' + SE.rating(upset.l.before) + '</span>).');
    }
    if (anon) {
      lines.push('<span class="' + MUTED + '">Gerechnet ohne ' + (anon === 1 ? 'den einen Schwinger, der nicht mit Namen veröffentlicht wird' :
        'die ' + SE.num(anon) + ' Schwinger, die nicht mit Namen veröffentlicht werden') + ' (für sie wird keine Wertung gezeigt).</span>');
    }
    return '<div class="se-card mt-3 space-y-1 text-sm"><h2 class="font-semibold">Das Fest in Zahlen</h2>' +
      lines.map(function (l) { return '<p>' + l + '</p>'; }).join('') + '</div>';
  }

  function renderFest(f, focusId) {
    document.title = f.name + ' – Schwinger-ELO';
    f.athleteRows = SE.table(f.athletes);
    f.athleteRows.forEach(function (a, i) { a.idx = i; });
    f.boutRows = SE.table(f.bouts);
    var rated = f.status !== 'unrated';
    /* contributions per Gang: only from a file that carries them (a browser may still
     * hold a festival file of an earlier build - then the page is as it was, without
     * numbers and without a remainder that would be wrong) */
    f.perGang = rated && f.bouts.cols.indexOf('d') !== -1;
    var state = { sort: 'pts', open: {} };
    f.athleteRows.forEach(function (a) { if (focusId && a.id === focusId) { state.open[a.idx] = true; } });
    var summary = rated ? summaryHtml(f) : '';

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
      html += summary;
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
          '<button type="button" class="se-link mt-0.5 whitespace-nowrap text-xs" data-toggle="' + a.idx + '" aria-expanded="' + open + '">' +
          (open ? 'Gänge ausblenden' : (f.perGang && a.after !== null ? 'Gänge und Beiträge zeigen' : 'Gänge zeigen')) + '</button></td>' +
          '<td class="se-td whitespace-nowrap text-right tabular-nums">' + a.w + '–' + a.d + '–' + a.l + '</td>' +
          '<td class="se-td text-right tabular-nums hidden sm:table-cell">' + (a.pts ? SE.num(a.pts, 2) : '–') + '</td>' +
          (rated ? '<td class="se-td whitespace-nowrap text-right tabular-nums">' +
            (a.after === null ? '–' : '<span class="text-xs text-stone-500 dark:text-stone-400">' + SE.rating(a.before) + ' → </span><span class="font-semibold">' + SE.rating(a.after) + '</span>') + '</td>' +
            '<td class="se-td text-right tabular-nums ' + (diff === null ? '' : upDown(Math.round(diff))) + '">' + SE.signed(diff) + '</td>' : '') +
          '</tr>';
        if (open) {
          html += '<tr class="' + (focus ? 'se-row-on' : '') + '"><td class="se-td text-sm" colspan="' + (rated ? 5 : 3) + '">' + boutsHtml(f, a) + '</td></tr>';
        }
      });
      html += '</tbody></table></div>' +
        '<p class="mt-3 text-xs text-stone-500 dark:text-stone-400">S–G–N: gewonnen – gestellt – verloren. Noten: Summe der gedruckten Noten, ' +
        'keine offizielle Rangliste (dafür die Quelle beachten). Wertung vor → nach dem Fest, +/− die Veränderung' +
        (f.perGang ? '; bei den geöffneten Gängen steht, was jeder Gang dazu beigetragen hat. Die Wertung wird pro Fest berechnet, gegen die Wertungen vor dem Fest – die Beiträge teilen diese Änderung auf die Gänge auf' : '') +
        '. Schwinger ohne veröffentlichten Namen stehen ohne Wertung da' +
        (f.perGang ? ', und bei Gängen gegen sie steht kein Beitrag in der Zeile' : '') + '. ' +
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
