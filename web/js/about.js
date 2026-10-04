/* Schwinger-ELO: fills the numbers of the methodology page from data/meta.json. */
(function () {
  'use strict';
  var SE = window.SE;

  function set(id, text) {
    var el = SE.$(id);
    if (el) { el.textContent = text; }
  }

  /* a list item with a bold lead; the text holds numbers of meta.json and is escaped */
  function item(id, lead, text) {
    var el = SE.$(id);
    if (el) { el.innerHTML = '<strong>' + SE.esc(lead) + '</strong> ' + SE.esc(text); }
  }

  function show(id) {
    var el = SE.$(id);
    if (el) { el.classList.remove('hidden'); }
  }

  SE.meta().then(function (meta) {
    var m = meta.model, c = meta.counts, pub = meta.publish || {};
    // non-public route for corrections and objections, only when one is configured
    var link = SE.$('se-contact-link');
    if (meta.contact && link && /^[^\s@<>"]+@[^\s@<>"]+$/.test(meta.contact)) {
      link.textContent = meta.contact;
      link.setAttribute('href', 'mailto:' + meta.contact);
      show('se-contact');
    }
    if (meta.empty) { return; }
    if (pub.min_age > 0 && pub.withheld_from_birth_year) {
      var n = pub.unknown_recent_seasons || 0;
      set('se-minors', 'Schwinger, die am Datenstand nicht sicher ' + pub.min_age + ' Jahre alt sind (Jahrgang ' +
        pub.withheld_from_birth_year + ' und jünger), werden nicht mit Namen veröffentlicht. ' +
        (n > 0 && pub.withheld_from_first_season
          ? 'Dasselbe gilt für Schwinger ohne bekannten Jahrgang, die erst seit ' + pub.withheld_from_first_season +
            ' antreten (die letzten ' + n + ' Saisons): Sie könnten minderjährig sein. Wer ohne bekannten Jahrgang schon länger antritt, ist aufgeführt. '
          : 'Wo der Jahrgang nicht bekannt ist, lässt sich das Alter nicht prüfen; diese Schwinger sind aufgeführt. ') +
        'Das betrifft ' + SE.num(c.withheld) + ' Schwinger' +
        (c.withheld_unknown ? ' (' + SE.num(c.withheld_unknown) + ' davon ohne Jahrgang)' : '') +
        ', von denen ' + SE.num(c.withheld_ranked) + ' sonst einen Rang hätten; die Ränge zählen deshalb nur die veröffentlichten Schwinger.');
      /* Says what the withholding does and what it does not (owner's decision of
       * 2026-10-04): no name, no profile, no rating shown - but the ratings are
       * calculated from public results and can be worked out from them. */
      set('se-minors2', 'Was das heisst: Diese Schwinger werden nicht mit Namen aufgeführt, haben kein Profil, keinen Eintrag in der Suche ' +
        'und keinen Rang, und für sie wird keine Wertung angezeigt. Ihre Gänge zählen aber für die Wertung und stehen beim Fest, ' +
        'weil sie auch die Gänge ihrer Gegner sind: Die Zeile heisst «Jungschwinger, Name nicht veröffentlicht» und zeigt ' +
        'gewonnen – gestellt – verloren, die Notensumme und die einzelnen Gänge, ohne Klub, Teilverband und Jahrgang.');
      set('se-minors3', 'Was das nicht heisst: Verborgen sind diese Schwinger damit nicht. Wer die verlinkte Resultatliste der Quelle ' +
        'daneben legt, kann eine solche Zeile einer Person zuordnen – dort stehen die Namen ohnehin. Auch ihre Wertungen werden ' +
        'aus den öffentlichen Resultaten berechnet, wie alle anderen. Sie werden hier nicht angezeigt, lassen sich aber ausrechnen: ' +
        'aus den Wertungen ihrer namentlich aufgeführten Gegner vor und nach einem Fest ziemlich genau, und mit dem öffentlichen ' +
        'Programm dieser Auswertung und den öffentlichen Resultatlisten vollständig. Im Vergleich steht zudem pro Fest, wie viel die ' +
        'Gänge gegen nicht genannte Schwinger zusammen zur Wertung eines aufgeführten Schwingers beigetragen haben.');
      show('se-minors3');
      show('se-minors2');
      show('se-minors');
    }
    set('se-facts', 'Datenstand ' + SE.date(meta.as_of) + ': ' + SE.num(c.bouts) + ' gewertete Gänge, ' + SE.num(c.festivals) +
      ' Feste mit Resultaten aus den Saisons ' + meta.first_season + ' bis ' + meta.last_season + ', ' + SE.num(c.athletes) + ' Schwinger, davon ' +
      SE.num(c.ranked) + ' mit Rang.');
    var k = m.k, groups = [
      ['Eidgenössische Anlässe', k.ESAF], ['Bergkranzfeste', k.Bergkranz], ['Teilverbandsfeste', k.Teilverband],
      ['Kantonal- und Gauverbandsfeste', k.Kantonal], ['Regionalfeste', k.Regional]
    ];
    /* the first seasons of the data, as far as the data say what they are */
    var first = meta.first_season, regional = meta.first_regional_season;
    if (first) {
      set('se-burnin', String(first));
      set('se-before', 'vor ' + first);
    }
    set('se-scale', SE.scaleText(meta));
    if (c.rated && c.birth_year_known !== undefined) {
      set('se-noby', ' (der Jahrgang bei rund ' + SE.num(100 - 100 * c.birth_year_known / c.rated) + ' %)');
    }
    if (first && regional && regional > first) {
      item('se-early-fests', 'Nur Kranzfeste.', 'Für ' + first + ' bis ' + (regional - 1) + ' sind nur Kranzfeste erfasst, rund 35 pro Saison. ' +
        'Regionalfeste kommen erst ' + regional + ' dazu (zuerst nur einzelne, ab ' + (regional + 1) + ' rund hundert pro Saison). ' +
        'Wer in den frühen Jahren nie an einem Kranzfest antrat, fehlt bis dahin.');
      item('se-early-sheets', 'Listen ohne die Ausgeschiedenen.', 'Die alten Listen drucken oft nur die vorderen Ränge oder nur die Schwinger, ' +
        'die alle Gänge bestritten haben. Wer früher ausschied, steht dann nur als Gegner in der Zeile eines anderen: mit Namen und Resultat, ohne eigene Note. ' +
        'Solche Gänge zählen voll für die Wertung' + (c.bouts_one_sided ? ' (' + SE.num(c.bouts_one_sided) + ' Gänge)' : '') + ', beim Fest stehen sie «ohne Note». ' +
        (c.name_only ? SE.num(c.name_only) + ' Schwinger sind nur als solche Gegner bekannt: Sie haben eine Wertung, aber nie einen Rang, weil zu wenige ihrer Gänge erfasst sind. ' : '') +
        'Ihre übrigen Gänge fehlen; ihre Bilanz sieht deshalb schlechter aus, als sie war.');
      item('se-early-ident', 'Wer ist wer?', 'Bis ' + (regional - 1) + ' nennen die Listen selten Klub oder Jahrgang. Die Gänge wurden einer Person meist allein über den Namen zugeordnet. ' +
        'Das ist weniger sicher als später: Zwei Schwinger gleichen Namens können in einer Laufbahn stecken, und eine Laufbahn mit langer Pause kann als zwei Schwinger erscheinen. ' +
        'Wo zwei Namensvetter in der Suche dieselben Angaben haben, steht die Zahl ihrer Feste dabei – mehr unterscheidet sie in den Quellen nicht.');
      item('se-early-gaps', 'Lücken.', 'Nicht jedes Kranzfest dieser Jahre hat eine lesbare Liste; aus der Südwestschweiz und dem Berner Jura fehlen besonders viele, oder es gibt nur Auszüge. ' +
        'Die Jahre vor ' + first + ' sind nicht gewertet, weil es von ihnen fast nur Auszüge der vordersten Ränge gibt. ' +
        first + ' selbst wird gewertet, hat aber keine Plätze (Einschwing-Saison).');
      show('se-early');
    }
    set('se-k', groups.filter(function (g) { return g[1]; }).map(function (g) {
      return g[0] + ' K = ' + SE.num(g[1]);
    }).join(', '));
    set('se-delta', 'um ' + SE.num(m.delta * 100) + ' % ihres Abstands');
    set('se-minbouts', String(m.provisional_min_bouts));
    set('se-minbouts2', String(m.provisional_min_bouts));
    set('se-seasonbouts', String(m.season_min_bouts));
    set('se-gaps', 'Von ' + SE.num(c.festivals + c.festivals_missing) + ' Festen haben ' + SE.num(c.festivals_partial) +
      ' Lücken (einzelne Zeilen der Liste liessen sich nicht übernehmen), bei ' + SE.num(c.festivals_missing) +
      ' gibt es keine lesbare Resultatliste.');
  }).catch(function () { /* the static text stays */ });
})();
