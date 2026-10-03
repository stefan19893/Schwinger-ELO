/* Schwinger-ELO: athlete profile (athlete.html?id=<athlete_id>). */
(function () {
  'use strict';
  var SE = window.SE;
  var view = SE.$('se-view');
  var id = SE.param('id');

  function card(label, value, sub) {
    return '<div class="se-card"><div class="text-xs text-stone-500 dark:text-stone-400">' + label + '</div>' +
      '<div class="mt-0.5 text-2xl font-bold tabular-nums">' + value + '</div>' +
      (sub ? '<div class="mt-0.5 text-xs text-stone-500 dark:text-stone-400">' + sub + '</div>' : '') + '</div>';
  }

  function has(h, reason) { return h.provisional.indexOf(reason) !== -1; }

  /* Idle athletes lead with peak and last-active rating: the stored current rating
   * keeps moving towards 1500 every season without a bout. */
  function cards(h) {
    var out = [];
    var peakSub = h.peak_date ? SE.esc(SE.date(h.peak_date)) +
      (h.peak_fest ? ', <a class="se-link" href="' + SE.festUrl(h.peak_fest.id, h.id) + '">' + SE.esc(h.peak_fest.name) + '</a>' : '') : '';
    var peak = h.peak !== null ? card('Bestwert', SE.rating(h.peak), peakSub) : '';
    if (has(h, 'inactive')) {
      if (peak) { out.push(peak); }
      out.push(card('Letzte Wertung', SE.rating(h.rating_last), 'nach dem letzten Fest, ' + SE.esc(SE.date(h.last_date))));
    } else {
      out.push(card('Aktuelle Wertung', SE.rating(h.rating),
        (h.ranked ? 'Rang ' + SE.num(h.rank) : 'ohne Rang (provisorisch)') +
        ', letzter Kampf ' + SE.esc(SE.date(h.last_date)) +
        (h.idle > 180 ? ' <span class="se-idle">' + SE.num(h.idle) + ' Tage vor dem Datenstand</span>' : '')));
      if (peak) { out.push(peak); }
    }
    if (h.record) {
      out.push(card('Bilanz', SE.num(h.record[0]) + '–' + SE.num(h.record[1]) + '–' + SE.num(h.record[2]),
        'gewonnen – gestellt – verloren, ' + SE.num(h.bouts) + ' Gänge an ' + SE.num(h.festivals) + ' Festen'));
    }
    return '<div class="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">' + out.join('') + '</div>';
  }

  function notes(h) {
    var out = [];
    if (has(h, 'inactive')) {
      out.push(SE.note('Seit dem ' + SE.esc(SE.date(h.last_date)) + ' ohne erfassten Kampf (' + SE.num(h.idle) +
        ' Tage bis zum Datenstand vom ' + SE.esc(SE.date(h.as_of)) + ') und deshalb nicht mehr in der Rangliste. ' +
        'Ohne Kämpfe wird die Wertung jede Saison ein Stück Richtung 1500 zurückgeführt; ' +
        'rechnerisch steht sie am Datenstand bei ' + SE.rating(h.rating) + '. Aussagekräftiger sind Bestwert und letzte Wertung.', 'info'));
    }
    if (has(h, 'few_bouts')) {
      out.push(SE.note('Provisorische Wertung: erst ' + SE.num(h.bouts) +
        ' gewertete Gänge. Mit so wenigen Gängen ist die Zahl noch wenig verlässlich; es gibt keinen Rang und keinen Bestwert.', 'info'));
    }
    if (h.unc) {
      out.push(SE.note('<strong>Identität unsicher.</strong> Bei ' + SE.num(h.unc_rows[0]) + ' von ' + SE.num(h.unc_rows[1]) +
        ' Festen liess sich nicht sicher entscheiden, welcher Namensvetter angetreten ist. ' +
        'Diese Laufbahn kann Gänge einer anderen Person enthalten oder eigene vermissen.'));
    }
    if (h.namesakes.length) {
      var list = h.namesakes.map(function (n) {
        return '<li><a class="se-link" href="' + SE.athleteUrl(n.id) + '">' + SE.esc(n.name) + '</a>' + SE.uncertainMark(n.unc) + ' <span class="text-xs">(' +
          (SE.subline(n, true) || 'ohne weitere Angaben') + ')</span></li>';
      }).join('');
      out.push('<details class="se-details"' + (h.namesakes.length <= 3 ? ' open' : '') + '><summary>Gleicher Name, anderer Schwinger (' +
        h.namesakes.length + ')</summary><ul class="mt-2 space-y-1">' + list + '</ul></details>');
    }
    return out.length ? '<div class="mt-4 space-y-3">' + out.join('') + '</div>' : '';
  }

  function seasons(h) {
    var rows = SE.table(h.seasons);
    if (!rows.length) { return ''; }
    var html = '<h2 class="mt-8 text-lg font-bold">Saisons</h2>' +
      '<div class="mt-2 overflow-x-auto"><table class="se-table"><thead><tr><th class="se-th">Saison</th>' +
      '<th class="se-th text-right">Platz</th><th class="se-th text-right">Wertung Ende</th>' +
      '<th class="se-th text-right">Höchstwert</th><th class="se-th text-right">Gänge</th></tr></thead><tbody>';
    rows.slice().reverse().forEach(function (s) {
      html += '<tr><td class="se-td tabular-nums"><a class="se-link" href="index.html#saison-' + SE.esc(s.season) + '">' +
        SE.esc(s.season) + '</a></td>' +
        '<td class="se-td text-right tabular-nums">' + (s.pos === null ? '–' : SE.num(s.pos)) + '</td>' +
        '<td class="se-td text-right tabular-nums font-semibold">' + SE.rating(s.rating) + '</td>' +
        '<td class="se-td text-right tabular-nums">' + SE.rating(s.peak) + '</td>' +
        '<td class="se-td text-right tabular-nums">' + SE.num(s.bouts) + '</td></tr>';
    });
    return html + '</tbody></table></div>' +
      '<p class="mt-2 text-xs text-stone-500 dark:text-stone-400">Platz in der Saisonrangliste; „–“ bei zu wenigen Gängen ' +
      'oder in Saisons ohne Rangierung (2011, 2020).</p>';
  }

  function festivals(h) {
    var rows = SE.table(h.history).reverse();
    var html = '<h2 class="mt-8 text-lg font-bold">Feste</h2>' +
      '<div class="mt-2 overflow-x-auto"><table class="se-table"><thead><tr><th class="se-th">Fest</th>' +
      '<th class="se-th text-right" title="Punkte: Sieg 1, Gestellter ½">Punkte</th>' +
      '<th class="se-th text-right">Wertung</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      var diff = r.after - r.before;
      html += '<tr><td class="se-td"><a class="se-link" href="' + SE.festUrl(r.fest_id, h.id) + '">' + SE.esc(r.fest) + '</a>' +
        '<span class="block text-xs text-stone-500 dark:text-stone-400"><span class="tabular-nums">' + SE.esc(SE.date(r.date)) + '</span> · ' +
        SE.esc(SE.category(r.cat)) + ((r.flags & 2) ? ' · Rückkehr nach langer Pause' : '') + '</span></td>' +
        '<td class="se-td whitespace-nowrap text-right tabular-nums">' + SE.num(r.score, 1) + ' / ' + SE.esc(r.n) +
        '<span class="block text-xs text-stone-500 dark:text-stone-400">erwartet ' + SE.num(r.exp, 1) + '</span></td>' +
        '<td class="se-td whitespace-nowrap text-right tabular-nums"><span class="font-semibold">' + SE.rating(r.after) + '</span>' +
        '<span class="block text-xs ' + (diff >= 0.5 ? 'se-up' : diff <= -0.5 ? 'se-down' : '') + '">' + SE.signed(diff) + '</span></td></tr>';
    });
    return html + '</tbody></table></div>' +
      '<p class="mt-2 text-xs text-stone-500 dark:text-stone-400">Punkte: Sieg 1, Gestellter ½; «erwartet» ist die Punktzahl, die nach den Wertungen ' +
      'vor dem Fest zu erwarten war. Wertung nach dem Fest, darunter die Veränderung an diesem Fest (ohne die Rückführung am 1. April).</p>';
  }

  function render(h) {
    document.title = h.name + ' – Schwinger-ELO';
    var badges = '';
    if (has(h, 'inactive')) { badges += ' <span class="se-badge se-badge-muted">nicht mehr aktiv</span>'; }
    if (has(h, 'few_bouts')) { badges += ' <span class="se-badge se-badge-muted">provisorisch</span>'; }
    if (h.unc) { badges += ' <span class="se-badge se-badge-warn">Identität unsicher</span>'; }
    var years = h.first ? (h.first === h.last ? 'Feste erfasst ' + SE.esc(h.first) : 'Feste erfasst ' + SE.esc(h.first) + '–' + SE.esc(h.last)) : '';
    var sub = [SE.subline(h), years].filter(Boolean).join(' · ');
    var html = '<h1 class="text-2xl font-bold tracking-tight">' + SE.esc(h.name) + badges + '</h1>' +
      '<p class="mt-1 text-sm text-stone-600 dark:text-stone-300">' + sub + '</p>' +
      cards(h) + notes(h) +
      '<h2 class="mt-8 text-lg font-bold">Verlauf</h2>' +
      '<div id="se-chart" class="mt-2 h-80 w-full sm:h-96" role="img" aria-label="Verlauf der ELO-Wertung von ' + SE.esc(h.name) +
      '; die Werte stehen in der Tabelle der Feste"></div>' +
      '<p class="mt-1 text-xs text-stone-500 dark:text-stone-400">Punkt = Wertung nach einem Fest (antippen für Details). ' +
      'Gestrichelt: Rückführung Richtung 1500 am Saisonwechsel (1. April), gehört zu keinem Fest.</p>' +
      seasons(h) + festivals(h) +
      (!h.club || !h.by ? '<p class="mt-6 text-xs text-stone-500 dark:text-stone-400">Klub oder Jahrgang fehlen in den Quellen für diesen Schwinger.</p>' : '') +
      (h.club ? '<p class="mt-2 text-xs text-stone-500 dark:text-stone-400">Klub: häufigster Klub in den Ranglisten der Laufbahn, nicht zwingend der heutige.</p>' : '');
    view.innerHTML = html;
    try {
      SE.careerChart(SE.$('se-chart'), h);
    } catch (err) {
      SE.$('se-chart').innerHTML = SE.note('Das Diagramm konnte nicht gezeichnet werden; die Werte stehen in der Tabelle unten.');
      SE.$('se-chart').className = 'mt-2';
    }
  }

  /* Ids can change when the data are rebuilt: offer a search from the words in the id. */
  function notFound(rawId) {
    var words = SE.norm(String(rawId || '').replace(/-p?\d+(-\d+)?$/, '').replace(/-/g, ' '));
    var html = '<h1 class="text-2xl font-bold tracking-tight">Schwinger nicht gefunden</h1>' +
      '<p class="mt-2 text-sm">' + (rawId ? 'Zu diesem Link gibt es kein Profil (mehr). Die Kennungen können sich ändern, wenn die Daten neu aufgebaut werden.'
        : 'Es wurde kein Schwinger angegeben.') + ' Bitte über die Suche oben oder in der <a class="se-link" href="index.html">Rangliste</a> nachschlagen.</p>' +
      '<div id="se-suggest" class="mt-4"></div>';
    view.innerHTML = html;
    if (!words) { return; }
    SE.loadSearchIndex().then(function (index) {
      var res = SE.search(index, words, 10);
      if (!res.total) { res = SE.search(index, words.split(' ')[0], 10); }
      if (!res.total) { return; }
      SE.$('se-suggest').innerHTML = '<h2 class="text-lg font-bold">Vielleicht gemeint</h2>' +
        '<div class="se-card mt-2 p-0">' + SE.searchResultsHtml(res) + '</div>';
    }).catch(function () { /* the search field still works */ });
  }

  if (!id || !SE.ID_RE.test(id)) {
    notFound(id);
  } else {
    SE.getJSON('data/history/history_' + id + '.json').then(render).catch(function (err) {
      if (err.status === 404) { notFound(id); } else { SE.showError(view, err); }
    });
  }
})();
