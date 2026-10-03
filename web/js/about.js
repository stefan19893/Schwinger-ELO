/* Schwinger-ELO: fills the numbers of the methodology page from data/meta.json. */
(function () {
  'use strict';
  var SE = window.SE;

  function set(id, text) {
    var el = SE.$(id);
    if (el) { el.textContent = text; }
  }

  SE.meta().then(function (meta) {
    var m = meta.model, c = meta.counts;
    if (meta.empty) { return; }
    set('se-facts', 'Datenstand ' + SE.date(meta.as_of) + ': ' + SE.num(c.bouts) + ' gewertete Gänge, ' + SE.num(c.festivals) +
      ' Feste mit Resultaten aus den Saisons ' + meta.first_season + ' bis ' + meta.last_season + ', ' + SE.num(c.athletes) + ' Schwinger, davon ' +
      SE.num(c.ranked) + ' mit Rang.');
    var k = m.k, groups = [
      ['Eidgenössische Anlässe', k.ESAF], ['Bergkranzfeste', k.Bergkranz], ['Teilverbandsfeste', k.Teilverband],
      ['Kantonal- und Gauverbandsfeste', k.Kantonal], ['Regionalfeste', k.Regional]
    ];
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
