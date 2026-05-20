/* ================================================================
   AUGUR — Chart Engine
   Wraps TradingView Lightweight Charts + Chart.js
   ================================================================ */

const ChartEngine = (() => {
  const COLORS = {
    green:   '#00ff9f',
    greenDim:'#00cc7a',
    red:     '#ff3355',
    blue:    '#00c8ff',
    amber:   '#ffaa00',
    purple:  '#9945ff',
    bg:      '#061520',
    bgElevated: '#0a2030',
    border:  '#0f2a3a',
    text:    '#c8d8e8',
    textDim: '#3a5a70',
  };

  const LW_THEME = {
    layout: {
      background: { type: 'solid', color: COLORS.bg },
      textColor: COLORS.textDim,
      fontFamily: "'JetBrains Mono', monospace",
      fontSize: 10,
    },
    grid: {
      vertLines: { color: COLORS.border, style: 1 },
      horzLines:  { color: COLORS.border, style: 1 },
    },
    crosshair: {
      mode: LightweightCharts?.CrosshairMode?.Normal ?? 1,
      vertLine: { color: COLORS.green + '60', labelBackgroundColor: COLORS.bgElevated },
      horzLine: { color: COLORS.green + '60', labelBackgroundColor: COLORS.bgElevated },
    },
    rightPriceScale: {
      borderColor: COLORS.border,
      textColor: COLORS.textDim,
    },
    timeScale: {
      borderColor: COLORS.border,
      textColor: COLORS.textDim,
      timeVisible: true,
      secondsVisible: false,
    },
  };

  // ── Active instances ──────────────────────────────────────────────
  const instances = {};

  function destroyChart(id) {
    if (instances[id]) {
      // Disconnect the ResizeObserver attached in createPriceChart before
      // .remove(); otherwise the RO keeps observing the (now-detached)
      // container and fires applyOptions on a removed chart on next resize.
      try {
        const ro = instances[id]._resizeObserver;
        if (ro && typeof ro.disconnect === 'function') ro.disconnect();
      } catch(e) {}
      try { instances[id].remove(); } catch(e) {}
      delete instances[id];
    }
  }

  // ── Candlestick / Line chart ──────────────────────────────────────
  function createPriceChart(containerId, data, opts = {}) {
    const container = document.getElementById(containerId);
    if (!container || !window.LightweightCharts) return null;
    destroyChart(containerId);

    const chart = LightweightCharts.createChart(container, {
      ...LW_THEME,
      width: container.clientWidth,
      height: opts.height || 380,
      handleScroll: true,
      handleScale: true,
    });

    instances[containerId] = chart;

    let series;
    if (opts.type === 'line' || data.length === 0 || !data[0]?.open) {
      series = chart.addLineSeries({
        color: COLORS.green,
        lineWidth: 1.5,
        crosshairMarkerRadius: 4,
        priceLineVisible: true,
        priceLineColor: COLORS.green + '40',
        lastValueVisible: true,
        priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
      });
      const lineData = data.map(d => ({ time: d.time, value: d.value ?? d.close }));
      series.setData(lineData);
    } else {
      series = chart.addCandlestickSeries({
        upColor: COLORS.green,
        downColor: COLORS.red,
        borderUpColor: COLORS.green,
        borderDownColor: COLORS.red,
        wickUpColor: COLORS.green,
        wickDownColor: COLORS.red,
      });
      series.setData(data);
    }

    // Volume histogram
    if (opts.showVolume !== false && data[0]?.volume !== undefined) {
      const volumeSeries = chart.addHistogramSeries({
        color: COLORS.green + '33',
        priceFormat: { type: 'volume' },
        priceScaleId: 'volume',
        scaleMargins: { top: 0.85, bottom: 0 },
      });
      chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
      volumeSeries.setData(data.map(d => ({
        time: d.time,
        value: d.volume || 0,
        color: d.close >= d.open ? COLORS.green + '44' : COLORS.red + '44',
      })));
    }

    // Resize observer
    const ro = new ResizeObserver(() => {
      chart.applyOptions({ width: container.clientWidth });
    });
    ro.observe(container);
    chart._resizeObserver = ro;

    return { chart, series };
  }

  // ── Overlay indicators on LW chart ───────────────────────────────
  function addSMAOverlay(chartRef, data, period, color) {
    if (!chartRef) return;
    const { chart } = chartRef;
    const closes = data.map(d => d.close || d.value);
    const smaData = [];
    for (let i = period - 1; i < closes.length; i++) {
      const sum = closes.slice(i - period + 1, i + 1).reduce((a, b) => a + b, 0);
      smaData.push({ time: data[i].time, value: parseFloat((sum / period).toFixed(4)) });
    }
    const smaSeries = chart.addLineSeries({
      color,
      lineWidth: 1,
      crosshairMarkerVisible: false,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    smaSeries.setData(smaData);
    return smaSeries;
  }

  // ── Portfolio Donut Chart ─────────────────────────────────────────
  function createAllocationChart(canvasId, labels, values, colors_array) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;

    if (ctx._chartInstance) { ctx._chartInstance.destroy(); }

    const defaultColors = [
      COLORS.green, COLORS.blue, COLORS.amber, COLORS.purple,
      COLORS.red, '#00ffff', '#ff9945', '#45ff99',
    ];

    const bgColors = (colors_array || defaultColors).map(c => c + 'bb');
    const borderColors = colors_array || defaultColors;

    const chart = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels,
        datasets: [{
          data: values,
          backgroundColor: bgColors,
          borderColor: borderColors,
          borderWidth: 1,
          hoverBorderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '72%',
        plugins: {
          legend: {
            position: 'bottom',
            labels: {
              color: COLORS.textDim,
              font: { family: 'JetBrains Mono', size: 10 },
              boxWidth: 10,
              padding: 12,
            },
          },
          tooltip: {
            backgroundColor: '#0a2030',
            borderColor: '#1a4060',
            borderWidth: 1,
            titleColor: COLORS.green,
            bodyColor: COLORS.text,
            titleFont: { family: 'JetBrains Mono', size: 11 },
            bodyFont:  { family: 'JetBrains Mono', size: 10 },
            callbacks: {
              label: ctx => ` ${ctx.label}: $${fmt.currency(ctx.parsed)} (${ctx.dataset.data.reduce((a,b)=>a+b,0) > 0 ? ((ctx.parsed / ctx.dataset.data.reduce((a,b)=>a+b,0))*100).toFixed(1) : 0}%)`,
            },
          },
        },
      },
    });

    ctx._chartInstance = chart;
    return chart;
  }

  // ── P&L Bar Chart ─────────────────────────────────────────────────
  function createPnlChart(canvasId, labels, pnl) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    if (ctx._chartInstance) { ctx._chartInstance.destroy(); }

    const colors = pnl.map(v => v >= 0 ? COLORS.green + 'bb' : COLORS.red + 'bb');
    const borderColors = pnl.map(v => v >= 0 ? COLORS.green : COLORS.red);

    const chart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'Unrealized P&L',
          data: pnl,
          backgroundColor: colors,
          borderColor: borderColors,
          borderWidth: 1,
          borderRadius: 1,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#0a2030',
            borderColor: '#1a4060',
            borderWidth: 1,
            titleColor: COLORS.green,
            bodyColor: COLORS.text,
            titleFont: { family: 'JetBrains Mono', size: 11 },
            bodyFont:  { family: 'JetBrains Mono', size: 10 },
            callbacks: {
              label: ctx => ` $${fmt.currency(ctx.parsed.y)}`,
            },
          },
        },
        scales: {
          x: {
            grid: { color: COLORS.border },
            ticks: { color: COLORS.textDim, font: { family: 'JetBrains Mono', size: 10 } },
          },
          y: {
            grid: { color: COLORS.border },
            ticks: {
              color: COLORS.textDim,
              font: { family: 'JetBrains Mono', size: 10 },
              callback: v => '$' + fmt.compact(v),
            },
          },
        },
      },
    });

    ctx._chartInstance = chart;
    return chart;
  }

  // ── Mini sparkline using Canvas 2D ────────────────────────────────
  function drawSparkline(canvas, data, color) {
    if (!canvas || !data || data.length < 2) return;
    const ctx = canvas.getContext('2d');
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    const min = Math.min(...data);
    const max = Math.max(...data);
    const range = max - min || 1;

    const pts = data.map((v, i) => ({
      x: (i / (data.length - 1)) * w,
      y: h - ((v - min) / range) * h,
    }));

    ctx.beginPath();
    ctx.moveTo(pts[0].x, pts[0].y);
    for (let i = 1; i < pts.length; i++) {
      ctx.lineTo(pts[i].x, pts[i].y);
    }
    ctx.strokeStyle = color || COLORS.green;
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Fill
    ctx.lineTo(pts[pts.length - 1].x, h);
    ctx.lineTo(pts[0].x, h);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, (color || COLORS.green) + '44');
    grad.addColorStop(1, (color || COLORS.green) + '00');
    ctx.fillStyle = grad;
    ctx.fill();
  }

  return { createPriceChart, createAllocationChart, createPnlChart, drawSparkline, addSMAOverlay, destroyChart };
})();
