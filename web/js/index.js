/* Schwinger-ELO: start page - current ranking, season lists, highest ratings. */
(function () {
  'use strict';
  var SE = window.SE;
  var PAGE = 50;
  var IDLE_WARN_DAYS = 180;

  var view = SE.$('se-view');
  var cache = {};
  var state = { tv: '', shown: PAGE };

  function load(name) {
    if (!cache[name]) {
      cache[name] = SE.getJSON('data/' + name + '.json');
      cache[name].catch(function () { delete cache[name]; });
    }
    return cache[name];
  }

  function route() {
    var h = window.location.hash.replace(/^#/, '');
    if (h.indexOf('saison') === 0) {
      var m = /^saison-(\d{4})$/.exec(h);
      return { tab: 'saison', season: m ? Number(m[1]) : null };
    }
    if (h === 'bestwerte') { return { tab: 'bestwerte' }; }
    return { tab: 'aktuell' };
  }

  function setTabs(tab) {
    var links = document.querySelectorAll('[data-tab]');
    for (var i = 0; i < links.length; i++) {
      var on = links[i].getAttribute('data-tab') === tab;
      links[i].classList.toggle('se-tab-on', on);
      if (on) { links[i].setAttribute('aria-current', 'page'); } else { links[i].removeAttribute('aria-current'); }
    }
  }

  function nameCell(o) {
    return SE.athleteLink(o) +
      '<span class="block text-xs text-stone-500 dark:text-stone-400">' + SE.subline(o) + '</span>';
  }

  // ------------------------------------------------------------------ current ranking
  /* `idle` is counted up to the data date (as are rank and the inactive rule), so the
   * text must not read as "until today": the month of the last bout, and the days as of
   * the "Stand" named above the table. */
  function lastBout(r, asOf) {
    if (r.idle > IDLE_WARN_DAYS) {
      return '<span class="se-idle" title="Letzter Kampf am ' + SE.esc(SE.date(r.last)) + ', ' + SE.num(r.idle) +
        ' Tage vor dem Datenstand vom ' + SE.esc(SE.date(asOf)) + '">' + SE.esc(SE.monthYear(r.last)) +
        '</span><span class="block text-xs text-stone-500 dark:text-stone-400">' + SE.num(r.idle) + ' Tage</span>';
    }
    return '<span title="Letzter Kampf am ' + SE.esc(SE.date(r.last)) + '">' + SE.esc(SE.monthYear(r.last)) + '</span>';
  }

  function renderCurrent(data, meta) {
    var rows = SE.table(data);
    var filtered = state.tv ? rows.filter(function (r) { return r.tv === state.tv; }) : rows;
    var m = meta.model;
    var html = '<p class="text-sm text-stone-600 dark:text-stone-300">Stand ' + SE.esc(SE.date(data.as_of)) + ': ' +
      SE.num(rows.length) + ' Schwinger mit mindestens ' + SE.esc(m.provisional_min_bouts) +
      ' gewerteten Gängen und einem Kampf in den letzten anderthalb Saisons.</p>';

    html += '<div class="mt-3 flex flex-wrap gap-2" role="group" aria-label="Teilverband">';
    [''].concat(Object.keys(SE.TV)).forEach(function (tv) {
      html += '<button type="button" class="se-chip' + (state.tv === tv ? ' se-chip-on' : '') +
        '" data-tv="' + SE.esc(tv) + '"' + (state.tv === tv ? ' aria-pressed="true"' : ' aria-pressed="false"') +
        (tv ? ' title="' + SE.esc(SE.TV[tv]) + '"' : '') + '>' + SE.esc(tv || 'Alle') + '</button>';
    });
    html += '</div>';
    if (state.tv) {
      html += '<div class="mt-3">' + SE.note('Gefiltert: ' + SE.esc(SE.TV[state.tv]) + '. Der Rang bleibt der Gesamtrang. ' +
        'Vergleiche zwischen Teilverbänden sind leicht verzerrt, weil die meisten Gänge innerhalb eines Teilverbands stattfinden ' +
        '(<a class="se-link" href="about.html#grenzen">Grenzen</a>).') + '</div>';
    }

    if (!filtered.length) {
      html += '<p class="mt-4 text-sm">Keine Schwinger in dieser Auswahl.</p>';
    } else {
      html += '<div class="mt-3 overflow-x-auto"><table class="se-table"><thead><tr>' +
        '<th class="se-th w-10 text-right">Rang</th><th class="se-th">Schwinger</th>' +
        '<th class="se-th text-right">Wertung</th>' +
        '<th class="se-th text-right hidden sm:table-cell">Bestwert</th>' +
        '<th class="se-th text-right">Letzter Kampf</th></tr></thead><tbody>';
      filtered.slice(0, state.shown).forEach(function (r) {
        html += '<tr><td class="se-td text-right tabular-nums text-stone-500 dark:text-stone-400">' + SE.num(r.rank) + '</td>' +
          '<td class="se-td">' + nameCell(r) + '</td>' +
          '<td class="se-td text-right tabular-nums font-semibold">' + SE.rating(r.rating) + '</td>' +
          '<td class="se-td text-right tabular-nums hidden sm:table-cell">' + SE.rating(r.peak) + '</td>' +
          '<td class="se-td text-right tabular-nums text-xs sm:text-sm">' + lastBout(r, data.as_of) + '</td></tr>';
      });
      html += '</tbody></table></div>';
      if (filtered.length > state.shown) {
        html += '<div class="mt-4 text-center"><button type="button" class="se-btn" id="se-more">Weitere ' +
          Math.min(PAGE, filtered.length - state.shown) + ' anzeigen (' + SE.num(filtered.length - state.shown) +
          ' übrig)</button></div>';
      }
    }

    html += '<details class="se-details mt-6"><summary>So ist die Rangliste zu lesen</summary>' +
      '<ul class="mt-2 list-disc space-y-1 pl-5">' +
      '<li>Die Wertung ist eine ELO-Zahl aus den Gängen seit ' + SE.esc(meta.first_season) +
      ': Sieg, Gestellter oder Niederlage gegen den erwarteten Ausgang, gewichtet nach Fest. Start bei ' +
      SE.rating(m.initial) + '.</li>' +
      '<li>Wer weniger als ' + SE.esc(m.provisional_min_bouts) + ' Gänge hat oder seit mehr als anderthalb Saisons nicht mehr angetreten ist, ' +
      'hat eine Wertung, aber keinen Rang. Über die Suche sind alle Schwinger zu finden.</li>' +
      '<li><span class="se-idle">Monat mit … Tagen</span> in der Spalte «Letzter Kampf»: Der letzte Kampf lag am Datenstand (' +
      SE.esc(SE.date(data.as_of)) + ') mehr als ' + IDLE_WARN_DAYS + ' Tage zurück (Verletzung, Pause, Rücktritt). ' +
      'Gezählt wird bis zum Datenstand, nicht bis heute. Die Wertung ist dann weniger aktuell.</li>' +
      '<li><abbr class="se-badge se-badge-warn">?</abbr> Identität unsicher: Namensvetter liessen sich in den Ranglisten nicht sicher trennen; ' +
      'die Wertung kann Gänge von zwei Personen enthalten.</li>' +
      '<li>Klub ist der häufigste Klub der Laufbahn, nicht zwingend der heutige. Klub und Jahrgang fehlen bei rund einem Viertel der Schwinger.</li>' +
      '</ul><p class="mt-2"><a class="se-link" href="about.html">Methodik und Grenzen</a></p></details>';

    view.innerHTML = html;
    var chips = view.querySelectorAll('[data-tv]');
    for (var i = 0; i < chips.length; i++) {
      chips[i].addEventListener('click', function () {
        state.tv = this.getAttribute('data-tv');
        state.shown = PAGE;
        renderCurrent(data, meta);
      });
    }
    var more = SE.$('se-more');
    if (more) {
      more.addEventListener('click', function () { state.shown += PAGE; renderCurrent(data, meta); });
    }
  }

  // ------------------------------------------------------------------ seasons
  var SEASON_NOTE = {
    burn_in: 'Einschwing-Saison: Es ist die erste Saison der Daten. ' +
      'Die Wertung startet für alle bei 1500 und muss sich erst einpendeln. ' +
      'Deshalb gibt es keine Rangierung.',
    thin: 'Saison mit sehr wenigen Festen (2020 fielen wegen der Pandemie fast alle aus, es blieben einige Hallenschwinget). ' +
      'Eine Saisonrangliste wäre nicht aussagekräftig, deshalb gibt es keine Rangierung.',
    current: 'Laufende Saison: Stand nach dem letzten erfassten Fest.'
  };

  function seasonTable(s, placed) {
    var html = '<div class="overflow-x-auto"><table class="se-table"><thead><tr>' +
      (placed ? '<th class="se-th w-10 text-right">Platz</th>' : '') +
      '<th class="se-th">Schwinger</th><th class="se-th text-right">Wertung<span class="hidden sm:inline"> Saisonende</span></th>' +
      '<th class="se-th text-right hidden sm:table-cell">Höchstwert</th>' +
      '<th class="se-th text-right">Gänge</th></tr></thead><tbody>';
    SE.table(s).forEach(function (r) {
      html += '<tr>' + (placed ? '<td class="se-td text-right tabular-nums text-stone-500 dark:text-stone-400">' + SE.num(r.pos) + '</td>' : '') +
        '<td class="se-td">' + nameCell(r) + '</td>' +
        '<td class="se-td text-right tabular-nums font-semibold">' + SE.rating(r.rating) + '</td>' +
        '<td class="se-td text-right tabular-nums hidden sm:table-cell">' + SE.rating(r.peak) + '</td>' +
        '<td class="se-td text-right tabular-nums">' + SE.num(r.bouts) + '</td></tr>';
    });
    return html + '</tbody></table></div>';
  }

  function renderSeason(data, wanted) {
    var seasons = data.seasons;
    if (!seasons.length) { view.innerHTML = '<p class="text-sm">Noch keine Saisons vorhanden.</p>'; return; }
    var s = seasons.filter(function (x) { return x.season === wanted; })[0];
    if (!s) {
      s = seasons.filter(function (x) { return x.status === 'ok' || x.status === 'current'; })[0] || seasons[0];
    }
    var placed = s.status === 'ok' || s.status === 'current';
    var html = '<label class="text-sm" for="se-season">Saison </label>' +
      '<select id="se-season" class="se-input">';
    seasons.forEach(function (x) {
      html += '<option value="' + SE.esc(x.season) + '"' + (x.season === s.season ? ' selected' : '') + '>' +
        SE.esc(x.season) + (x.status === 'burn_in' || x.status === 'thin' ? ' (ohne Rangierung)' : '') + '</option>';
    });
    html += '</select>';
    html += '<p class="mt-3 text-sm text-stone-600 dark:text-stone-300">' + SE.num(s.n_festivals) + (s.n_festivals === 1 ? ' gewertetes Fest, ' : ' gewertete Feste, ') +
      SE.num(s.n_athletes) + ' Schwinger im Einsatz' +
      (s.peak && placed ? '. Höchste Wertung der Saison: <a class="se-link" href="' + SE.athleteUrl(s.peak.id) + '">' +
        SE.esc(s.peak.name) + '</a>' + SE.uncertainMark(s.peak.unc) + ' (' + SE.rating(s.peak.rating) + ')' : '') + '.</p>';
    if (SEASON_NOTE[s.status]) {
      html += '<div class="mt-3">' + SE.note(SEASON_NOTE[s.status], s.status === 'current' ? 'info' : '') + '</div>';
    }
    if (!s.rows.length) {
      html += '<p class="mt-4 text-sm">Noch kein Schwinger hat in dieser Saison ' + SE.esc(data.min_bouts) + ' Gänge.</p>';
    } else if (placed) {
      html += '<div class="mt-3">' + seasonTable(s, true) + '</div>';
    } else {
      html += '<details class="se-details mt-4"><summary>Wertungen trotzdem ansehen (ohne Rangierung)</summary>' +
        '<div class="mt-2">' + seasonTable(s, false) + '</div></details>';
    }
    html += '<p class="mt-4 text-xs text-stone-500 dark:text-stone-400">Wertung am Saisonende, nur Schwinger mit mindestens ' +
      SE.esc(data.min_bouts) + ' Gängen in der Saison und genügend Gängen insgesamt; die besten ' + SE.num(s.rows.length) +
      ' von ' + SE.num(s.n_listed) + '. Höchstwert = beste Wertung nach einem Fest der Saison.</p>';
    view.innerHTML = html;
    SE.$('se-season').addEventListener('change', function () {
      window.location.hash = 'saison-' + this.value;
    });
  }

  // ------------------------------------------------------------------ all-time peaks
  function renderPeaks(data, meta) {
    var rows = SE.table(data);
    var html = SE.note('<strong>Keine Bestenliste aller Zeiten.</strong> ' +
      (meta.first_season ? 'Die Daten beginnen ' + SE.esc(meta.first_season) + '. ' : '') + SE.scaleText(meta) +
      ' Höchstwerte der letzten Jahre stehen deshalb vor den früheren, unabhängig davon, wer besser war. ' +
      'Auch Vergleiche zwischen Teilverbänden sind leicht verzerrt. ' +
      '<a class="se-link" href="about.html#grenzen">Mehr dazu</a>');
    if (!rows.length) {
      view.innerHTML = html + '<p class="mt-4 text-sm">Noch keine Bestwerte vorhanden.</p>';
      return;
    }
    html += '<div class="mt-3 overflow-x-auto"><table class="se-table"><thead><tr>' +
      '<th class="se-th w-10 text-right">Nr.</th><th class="se-th">Schwinger, erreicht am</th>' +
      '<th class="se-th text-right">Bestwert</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      r.unc = (r.flags & SE.F_UNCERTAIN) ? 1 : 0;
      html += '<tr><td class="se-td text-right tabular-nums text-stone-500 dark:text-stone-400">' + SE.num(r.pos) + '</td>' +
        '<td class="se-td">' + nameCell(r) +
        '<span class="block text-xs text-stone-500 dark:text-stone-400"><span class="tabular-nums">' + SE.esc(SE.date(r.date)) + '</span>, ' +
        '<a class="se-link" href="' + SE.festUrl(r.fest_id, r.id) + '">' + SE.esc(r.fest) + '</a></span></td>' +
        '<td class="se-td text-right tabular-nums font-semibold">' + SE.rating(r.peak) + '</td></tr>';
    });
    html += '</tbody></table></div>' +
      '<p class="mt-4 text-xs text-stone-500 dark:text-stone-400">Die ' + SE.num(rows.length) +
      ' höchsten je erreichten Wertungen (nach einem Fest' +
      (meta.model && meta.model.first_ranked_season ? ', ab der Saison ' + SE.esc(meta.model.first_ranked_season) : '') +
      ', erst ab genügend Gängen).</p>';
    view.innerHTML = html;
  }

  // ------------------------------------------------------------------ router
  function render() {
    var r = route();
    setTabs(r.tab);
    view.innerHTML = '<p class="text-sm text-stone-500 dark:text-stone-400">Wird geladen …</p>';
    var job;
    if (r.tab === 'saison') {
      job = load('seasons').then(function (d) { renderSeason(d, r.season); });
    } else if (r.tab === 'bestwerte') {
      job = Promise.all([load('alltime_top200'), SE.meta()]).then(function (res) { renderPeaks(res[0], res[1]); });
    } else {
      job = Promise.all([load('rankings_latest'), SE.meta()]).then(function (res) {
        if (!res[0].rows.length) {
          view.innerHTML = '<p class="text-sm">Noch keine Rangliste vorhanden.</p>';
          return;
        }
        renderCurrent(res[0], res[1]);
      });
    }
    job.catch(function (err) { SE.showError(view, err); });
  }

  window.addEventListener('hashchange', render);
  render();
})();
