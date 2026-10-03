/* Schwinger-ELO: career chart (ECharts).
 *
 * The line goes through "before" and "after" of every festival, so a festival is a
 * vertical step. The mean reversion on 1 April lies between two festivals: it is drawn
 * as its own dashed step on that date and never attributed to a festival. After the
 * last festival the stored rating keeps reverting; that tail is drawn dashed as well. */
(function () {
  'use strict';
  var SE = window.SE;

  /* 1 April dates x with d0 < x <= d1 (ISO strings compare like dates). */
  function seasonStarts(d0, d1, month) {
    var out = [], mm = (month < 10 ? '0' : '') + month;
    for (var y = Number(d0.slice(0, 4)); y <= Number(d1.slice(0, 4)); y++) {
      var x = y + '-' + mm + '-01';
      if (x > d0 && x <= d1) { out.push(x); }
    }
    return out;
  }

  /* Pure: history file -> the three data arrays of the chart. */
  SE.careerSeries = function (h) {
    var rows = SE.table(h.history);
    var delta = h.rev[0], mean = h.rev[1], month = h.rev[2];
    var line = [], reversion = [], points = [], tail = [];
    var prev = null;

    function steps(from, fromRating, to, toRating, lineOut) {
      var dates = seasonStarts(from, to, month), cur = fromRating;
      dates.forEach(function (x, i) {
        var next = (i === dates.length - 1 && toRating !== null) ? toRating : mean + (cur - mean) * (1 - delta);
        lineOut.push([x, cur]);
        if (lineOut === line) {
          line.push([x, null]);
          reversion.push([x, cur], [x, next], [x, null]);
        }
        lineOut.push([x, next]);
        cur = next;
      });
      return cur;
    }

    rows.forEach(function (r) {
      if (prev) { steps(prev.date, prev.after, r.date, r.before, line); }
      line.push([r.date, r.before]);
      line.push([r.date, r.after]);
      points.push({ value: [r.date, r.after], row: r });
      prev = r;
    });
    if (prev && h.as_of && h.as_of > prev.date) {
      tail.push([prev.date, prev.after]);
      steps(prev.date, prev.after, h.as_of, h.rating, tail);
      tail.push([h.as_of, h.rating]);
    }
    return { line: line, reversion: reversion, points: points, tail: tail };
  };

  function palette() {
    var dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    return dark
      ? { text: '#d6d3d1', grid: '#44403c', line: '#f87171', rev: '#38bdf8', muted: '#a8a29e', bg: '#1c1917', border: '#57534e' }
      : { text: '#44403c', grid: '#e7e5e4', line: '#b91c1c', rev: '#0369a1', muted: '#78716c', bg: '#ffffff', border: '#d6d3d1' };
  }

  function tooltip(p) {
    var r = p.data && p.data.row;
    if (!r) { return ''; }
    return '<strong>' + SE.esc(r.fest) + '</strong><br>' + SE.esc(SE.date(r.date)) + ' · ' +
      SE.esc(SE.category(r.cat)) + '<br>Wertung ' + SE.rating(r.before) + ' → <strong>' + SE.rating(r.after) +
      '</strong> (' + SE.signed(r.after - r.before) + ')<br>' + SE.num(r.score, 1) + ' Punkte aus ' + SE.esc(r.n) +
      ' Gängen, erwartet ' + SE.num(r.exp, 1);
  }

  function option(h, s) {
    var c = palette();
    var n = s.points.length;
    var size = n > 120 ? 4 : n > 40 ? 5 : 7;
    var showTail = s.tail.length && h.idle > 60;
    return {
      animation: false,
      textStyle: { color: c.text, fontFamily: 'inherit' },
      grid: { left: 44, right: 14, top: 34, bottom: 62 },
      legend: {
        top: 0, left: 0, itemWidth: 18, icon: 'roundRect', itemHeight: 3, selectedMode: false,
        textStyle: { color: c.text, fontSize: 11 },
        data: ['Wertung', 'Saisonwechsel (1. April)'].concat(showTail ? ['ohne Kampf'] : [])
      },
      tooltip: {
        trigger: 'item', confine: true, backgroundColor: c.bg, borderColor: c.border,
        textStyle: { color: c.text, fontSize: 12 }, formatter: tooltip
      },
      xAxis: {
        type: 'time', axisLine: { lineStyle: { color: c.border } },
        axisLabel: { color: c.muted, hideOverlap: true }, splitLine: { show: false }
      },
      yAxis: {
        type: 'value', scale: true,
        axisLabel: { color: c.muted, formatter: function (v) { return String(v); } },
        splitLine: { lineStyle: { color: c.grid } }
      },
      dataZoom: [{
        type: 'slider', height: 20, bottom: 8, borderColor: c.border, textStyle: { color: c.muted },
        fillerColor: 'rgba(185,28,28,0.15)', brushSelect: false, showDataShadow: false
      }],
      series: [
        {
          name: 'Wertung', type: 'line', data: s.line, showSymbol: false, silent: true, z: 2,
          connectNulls: false, lineStyle: { color: c.line, width: 1.5 }, itemStyle: { color: c.line },
          markLine: {
            silent: true, symbol: 'none', lineStyle: { color: c.muted, type: 'dotted' },
            label: { formatter: 'Start 1500', color: c.muted, fontSize: 10, position: 'insideEndTop' },
            data: [{ yAxis: h.rev[1] }]
          }
        },
        {
          name: 'Saisonwechsel (1. April)', type: 'line', data: s.reversion, showSymbol: false, silent: true, z: 4,
          connectNulls: false, lineStyle: { color: c.rev, width: 2, type: [3, 2] }, itemStyle: { color: c.rev }
        },
        {
          name: 'ohne Kampf', type: 'line', data: s.tail, showSymbol: false, silent: true, z: 2,
          lineStyle: { color: c.muted, width: 1.4, type: 'dotted' }, itemStyle: { color: c.muted }
        },
        {
          name: 'Fest', type: 'scatter', data: s.points, symbolSize: size, z: 3,
          itemStyle: { color: c.line }, emphasis: { scale: 2.5 }
        }
      ]
    };
  }

  SE.careerChart = function (el, h) {
    if (!window.echarts) { throw new Error('ECharts nicht geladen'); }
    var series = SE.careerSeries(h);
    var chart = window.echarts.init(el, null, { renderer: 'canvas' });
    function draw() { chart.setOption(option(h, series), true); }
    draw();
    window.addEventListener('resize', function () { chart.resize(); });
    if (window.matchMedia) {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      if (mq.addEventListener) { mq.addEventListener('change', draw); }
    }
    return chart;
  };
})();
