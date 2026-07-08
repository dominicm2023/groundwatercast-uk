// SVG chart renderers for the detail panel — zero dependencies.
// Each returns an <svg> string sized to a 0..W × 0..H viewBox; CSS scales
// width to the panel.
(function () {
  "use strict";
  const SVGNS = "http://www.w3.org/2000/svg";
  const PAL = (window.GWC_CONFIG && window.GWC_CONFIG.palette) || {
    below: "#d4a017", near: "#b5b5b5", above: "#1f77b4", none: "#cfcfcf",
  };

  function esc(s) {
    return String(s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  const fmt1 = (v) => (v == null || isNaN(v) ? "–" : (+v).toFixed(1));
  const dnum = (d) => new Date(d + "T00:00:00Z").getTime();

  // -- nice-ish y range with a little padding --------------------------------
  function yRange(vals) {
    const xs = vals.filter((v) => v != null && isFinite(v));
    let lo = Math.min(...xs), hi = Math.max(...xs);
    if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 1; }
    if (lo === hi) { lo -= 0.5; hi += 0.5; }
    const pad = (hi - lo) * 0.08;
    return [lo - pad, hi + pad];
  }

  // ==========================================================================
  // Fan chart: observed tail → P10–P90 band → P50 → threshold line → seasonal
  // P50 circles. `detail` is a stations/<id>.json object.
  // ==========================================================================
  // Geometry + data of the last-rendered fan, used by attachHover().
  let _fanCtx = null;

  function fanChart(detail, opts) {
    opts = opts || {};
    // The standalone /b/ page renders a bigger, page-proportioned chart (near
    // 1:1 with its container) so fonts/strokes aren't blown up like the panel's
    // small viewBox stretched to full width.
    const large = !!opts.large;
    const W = large ? 760 : 340, H = large ? 300 : 188;
    const m = large ? { l: 48, r: 16, t: 16, b: 30 } : { l: 38, r: 8, t: 10, b: 22 };
    const FS_Y = large ? 11 : 9, FS_X = large ? 10.5 : 8.5, FS_M = large ? 10 : 8;
    const iw = W - m.l - m.r, ih = H - m.t - m.b;

    const fanAll = (detail.forecast && detail.forecast.fan) || [];
    const nowcast = fanAll.filter((f) => f.segment === "nowcast");
    const fan = fanAll.filter((f) => f.segment !== "nowcast");   // forecast segment
    const fanFirst = fanAll[0] || null;                          // earliest fan point (nowcast start when stale)
    const histDays = opts.historyDays || 365;
    let obs = (detail.observed && detail.observed.series) || [];
    if (obs.length && fanFirst) {
      const cutoff = dnum(fanFirst.date) - histDays * 86400000;
      obs = obs.filter((p) => dnum(p[0]) >= cutoff);
    }
    if (!fanAll.length && !obs.length) { _fanCtx = null; return ""; }

    const seas = (detail.seasonal && detail.seasonal.months) || [];

    const xsAll = [];
    obs.forEach((p) => xsAll.push(dnum(p[0])));
    fanAll.forEach((f) => xsAll.push(dnum(f.date)));
    seas.forEach((s) => s.month_start && xsAll.push(dnum(s.month_start) + 14 * 86400000));
    const x0 = Math.min(...xsAll), x1 = Math.max(...xsAll);
    const X = (t) => m.l + ((t - x0) / (x1 - x0 || 1)) * iw;

    const ysAll = [];
    obs.forEach((p) => ysAll.push(p[1]));
    fanAll.forEach((f) => { ysAll.push(f.p10); ysAll.push(f.p90); });
    seas.forEach((s) => {
      ["gw_p10", "gw_p50", "gw_p90"].forEach((k) => {
        if (s[k] != null) ysAll.push(s[k]);
      });
    });
    const thr = detail.forecast && detail.forecast.threshold;
    if (thr != null) ysAll.push(thr);
    // User-set trigger levels (the ladder) — included so the chart auto-scales
    // to fit them, then drawn below.
    const levels = Array.isArray(opts.levels)
      ? opts.levels.filter((l) => l && isFinite(+l.level_mAOD)) : [];
    levels.forEach((l) => ysAll.push(+l.level_mAOD));
    const [ylo, yhi] = yRange(ysAll);
    const Y = (v) => m.t + (1 - (v - ylo) / (yhi - ylo || 1)) * ih;

    const parts = [];
    // axes
    parts.push(`<line x1="${m.l}" y1="${m.t}" x2="${m.l}" y2="${m.t + ih}" stroke="#d8dce1"/>`);
    parts.push(`<line x1="${m.l}" y1="${m.t + ih}" x2="${m.l + iw}" y2="${m.t + ih}" stroke="#d8dce1"/>`);
    const nY = large ? 4 : 2;
    for (let i = 0; i <= nY; i++) {
      const v = ylo + (i / nY) * (yhi - ylo), y = Y(v);
      parts.push(`<line x1="${m.l - 3}" y1="${y}" x2="${m.l + iw}" y2="${y}" stroke="#eef1f4"/>`);
      parts.push(`<text x="${m.l - 5}" y="${y + 3}" text-anchor="end" font-size="${FS_Y}" fill="#6b7280">${fmt1(v)}</text>`);
    }

    // The nowcast segment (drawn below) fills the last-obs -> today gap with a
    // modelled estimate on observed rainfall, replacing the old cosmetic dashed
    // bridge. Keep the last observed point so the history joins cleanly into it.
    const lastObs = obs.length ? obs[obs.length - 1] : null;

    // fan band (p10..p90)
    if (fan.length) {
      const top = fan.map((f) => `${X(dnum(f.date)).toFixed(1)},${Y(f.p90).toFixed(1)}`);
      const bot = fan.map((f) => `${X(dnum(f.date)).toFixed(1)},${Y(f.p10).toFixed(1)}`).reverse();
      parts.push(`<polygon points="${top.concat(bot).join(" ")}" fill="${PAL.above}" fill-opacity="0.16" stroke="none"/>`);
      // (The reduced-form roll cross-check stays in the pack as roll_p50 + the
      // model_spread confidence signal, but isn't drawn here — a competing
      // second line confuses the public view and the roll is the weaker model.)
      const p50 = fan.map((f) => `${X(dnum(f.date)).toFixed(1)},${Y(f.p50).toFixed(1)}`).join(" ");
      parts.push(`<polyline points="${p50}" fill="none" stroke="${PAL.above}" stroke-width="1.6"/>`);
    }

    // nowcast: the modelled gap (last obs -> today) on observed rainfall, drawn
    // distinctly (lighter band + dashed P50) so it never reads as observed data.
    // Pinned at the last reading (band ~0) and widening to meet the forecast at
    // today. Prepend the last obs point so it flows out of the dark history line.
    if (nowcast.length) {
      const nc = (lastObs ? [{ date: lastObs[0], p10: lastObs[1], p50: lastObs[1], p90: lastObs[1] }] : [])
        .concat(nowcast);
      const top = nc.map((f) => `${X(dnum(f.date)).toFixed(1)},${Y(f.p90).toFixed(1)}`);
      const bot = nc.map((f) => `${X(dnum(f.date)).toFixed(1)},${Y(f.p10).toFixed(1)}`).reverse();
      parts.push(`<polygon points="${top.concat(bot).join(" ")}" fill="${PAL.above}" fill-opacity="0.09" stroke="none"/>`);
      // P50 line runs obs -> nowcast and joins the forecast start, so the
      // observed->forecast-rainfall handoff reads as a continuous line, not a notch.
      const ncLine = fan.length ? nc.concat([fan[0]]) : nc;
      const ncp50 = ncLine.map((f) => `${X(dnum(f.date)).toFixed(1)},${Y(f.p50).toFixed(1)}`).join(" ");
      parts.push(`<polyline points="${ncp50}" fill="none" stroke="${PAL.above}" stroke-width="1.3" stroke-dasharray="3 2" stroke-opacity="0.85"/>`);
    }

    // observed tail
    if (obs.length) {
      const ol = obs.map((p) => `${X(dnum(p[0])).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ");
      parts.push(`<polyline points="${ol}" fill="none" stroke="#33414f" stroke-width="1.3"/>`);
    }

    // seasonal monthly P50: a dotted connector (continuing from the end of the
    // daily fan) through coarse monthly circles — coarse markers = monthly
    // means, not daily levels.
    // Most-likely tercile per month — colours the P50 circle to match the map
    // vocabulary (below=amber / near=grey / above=blue). Null probs → neutral.
    const tercileOf = (s) => {
      const probs = { below: s.p_below, near: s.p_near, above: s.p_above };
      let best = null, bv = -1;
      for (const k in probs) {
        if (probs[k] != null && isFinite(probs[k]) && probs[k] > bv) { bv = probs[k]; best = k; }
      }
      return best;
    };
    const seasPts = seas
      .filter((s) => s.gw_p50 != null && s.month_start)
      .map((s) => ({
        x: X(dnum(s.month_start) + 14 * 86400000),
        y: Y(s.gw_p50),
        y10: s.gw_p10 != null ? Y(s.gw_p10) : null,
        y90: s.gw_p90 != null ? Y(s.gw_p90) : null,
        tc: tercileOf(s),
      }));
    if (seasPts.length) {
      // P10–P90 whiskers: seasonal uncertainty, legitimately tight in the near
      // (recession-dominated) months and widening into autumn/winter as
      // year-to-year recharge diverges. (Skipped where the spread is sub-pixel.)
      const cap = 2.2;
      seasPts.forEach((p) => {
        if (p.y10 == null || p.y90 == null || Math.abs(p.y10 - p.y90) < 0.6) return;
        parts.push(`<line x1="${p.x.toFixed(1)}" y1="${p.y90.toFixed(1)}" x2="${p.x.toFixed(1)}" y2="${p.y10.toFixed(1)}" stroke="${PAL.above}" stroke-width="1" stroke-opacity="0.4"/>`);
        [p.y10, p.y90].forEach((yc) =>
          parts.push(`<line x1="${(p.x - cap).toFixed(1)}" y1="${yc.toFixed(1)}" x2="${(p.x + cap).toFixed(1)}" y2="${yc.toFixed(1)}" stroke="${PAL.above}" stroke-width="1" stroke-opacity="0.4"/>`));
      });
      // dotted connector through the P50 points (continuing from the fan end)
      const linePts = [];
      if (fan.length) {
        const lf = fan[fan.length - 1];
        linePts.push({ x: X(dnum(lf.date)), y: Y(lf.p50) });
      }
      linePts.push(...seasPts.map((p) => ({ x: p.x, y: p.y })));
      const pl = linePts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
      parts.push(`<polyline points="${pl}" fill="none" stroke="${PAL.above}" stroke-width="1" stroke-dasharray="2 3" stroke-opacity="0.55"/>`);
      // P50 circles — stroked in the month's most-likely tercile colour (the
      // map vocabulary: amber below / grey near / blue above), white-filled so
      // they read cleanly over the whiskers. Neutral blue when probs are null.
      seasPts.forEach((p) => {
        const col = (p.tc && PAL[p.tc]) || PAL.above;
        parts.push(`<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${large ? 3.6 : 2.4}" fill="#fff" stroke="${col}" stroke-width="${large ? 1.8 : 1.4}"/>`);
      });
    }

    // markers: the last real reading (nowcast anchor) and "today" (forecast
    // start). Both lines always; the "last reading" label is dropped when the
    // two are too close to label without colliding (e.g. the All view).
    const origin = detail.forecast && detail.forecast.origin_date;
    const vmark = (t, label) => {
      const sx = X(dnum(t));
      parts.push(`<line x1="${sx.toFixed(1)}" y1="${m.t}" x2="${sx.toFixed(1)}" y2="${m.t + ih}" stroke="#9aa0a6" stroke-width="0.8" stroke-dasharray="1 2"/>`);
      if (label) parts.push(`<text x="${sx.toFixed(1)}" y="${m.t - 2}" text-anchor="middle" font-size="${FS_M}" fill="#9aa0a6">${label}</text>`);
    };
    if (nowcast.length && origin && fan.length) {
      const room = Math.abs(X(dnum(fan[0].date)) - X(dnum(origin))) > 40;
      vmark(origin, room ? "last reading" : "");
      vmark(fan[0].date, "today");
    }

    // threshold line
    if (thr != null) {
      const y = Y(thr);
      parts.push(`<line x1="${m.l}" y1="${y}" x2="${m.l + iw}" y2="${y}" stroke="#c0392b" stroke-width="1" stroke-dasharray="4 3"/>`);
      parts.push(`<text x="${m.l + iw}" y="${y - 3}" text-anchor="end" font-size="${FS_X}" fill="#c0392b">threshold ${fmt1(thr)}</text>`);
    }

    // User trigger levels (the ladder) — distinct purple dashed lines, labelled
    // on the LEFT so they never collide with the red published-threshold label.
    levels.forEach((l) => {
      const y = Math.max(m.t, Math.min(m.t + ih, Y(+l.level_mAOD)));
      parts.push(`<line x1="${m.l}" y1="${y.toFixed(1)}" x2="${m.l + iw}" y2="${y.toFixed(1)}" stroke="#6a3d9a" stroke-width="1" stroke-dasharray="2 2"/>`);
      parts.push(`<text x="${m.l + 2}" y="${(y - 3).toFixed(1)}" text-anchor="start" font-size="${FS_M}" fill="#5b2d86">${esc(l.label)} ${fmt1(+l.level_mAOD)}</text>`);
    });

    // x labels (year-aware — the window can span several years)
    const lab = (t, anchor) =>
      `<text x="${X(t).toFixed(1)}" y="${m.t + ih + (large ? 16 : 13)}" text-anchor="${anchor}" font-size="${FS_X}" fill="#6b7280">${axisDate(t)}</text>`;
    if (large) {
      const N = 4;
      for (let i = 0; i <= N; i++) {
        const t = x0 + (i / N) * (x1 - x0);
        parts.push(lab(t, i === 0 ? "start" : i === N ? "end" : "middle"));
      }
    } else {
      parts.push(lab(x0, "start"));
      parts.push(lab(x1, "end"));
    }

    // hover layer (populated by attachHover)
    parts.push(`<g class="hoverlayer" style="pointer-events:none"></g>`);

    _fanCtx = { W, H, m, iw, ih, x0, x1, ylo, yhi, obs, fan: fanAll, seas, thr };

    // Real forecast horizon for the screen-reader label.
    const _hd = detail.forecast && detail.forecast.horizon_days;
    const _hLbl = _hd != null ? _hd : 15;
    return `<svg class="svg-chart svg-fan" viewBox="0 0 ${W} ${H}" role="img" ` +
      `aria-label="Groundwater level: observed history and ${_hLbl}-day forecast fan, continuing as a seasonal outlook">${parts.join("")}</svg>`;
  }

  // Attach a hover crosshair + value readout to a rendered fan <svg>, using
  // the geometry stashed by the matching fanChart() call.
  function attachFanHover(svg) {
    if (!svg || !_fanCtx) return null;
    const ctx = _fanCtx;
    const layer = svg.querySelector(".hoverlayer");
    const pt = svg.createSVGPoint();
    const invX = (px) => ctx.x0 + ((px - ctx.m.l) / ctx.iw) * (ctx.x1 - ctx.x0);
    const X = (t) => ctx.m.l + ((t - ctx.x0) / (ctx.x1 - ctx.x0 || 1)) * ctx.iw;
    const Y = (v) => ctx.m.t + (1 - (v - ctx.ylo) / (ctx.yhi - ctx.ylo || 1)) * ctx.ih;
    const nearest = (arr, t, getT) => {
      let best = null, bd = Infinity;
      for (const a of arr) { const d = Math.abs(getT(a) - t); if (d < bd) { bd = d; best = a; } }
      return { item: best, dist: bd };
    };
    let pinnedT = null;   // a scrub position that survives mouse-leave (slider-driven)

    // Which series sits under time t → {markX, markY, rows}. rows[0] is a date
    // header ([label, ""]); the rest are [key, value] pairs. Shared by the hover
    // handler and the programmatic scrubber.
    function rowsAt(t) {
      const fanStart = ctx.fan.length ? dnum(ctx.fan[0].date) : Infinity;
      const rows = [];
      let markY = null, markX = X(t);
      if (t >= fanStart - 0.5 * 86400000 && ctx.fan.length) {
        const r = nearest(ctx.fan, t, (f) => dnum(f.date));
        const f = r.item;
        const seasMax = ctx.fan.length ? dnum(ctx.fan[ctx.fan.length - 1].date) : -Infinity;
        if (t > seasMax + 16 * 86400000 && ctx.seas.length) {
          const sr = nearest(ctx.seas.filter((s) => s.month_start),
            t, (s) => dnum(s.month_start) + 14 * 86400000);
          if (sr.item) {
            markX = X(dnum(sr.item.month_start) + 14 * 86400000);
            markY = Y(sr.item.gw_p50);
            rows.push([monthLabel(sr.item) + " · seasonal", ""]);
            rows.push(["Median", fmt1(sr.item.gw_p50) + " mAOD"]);
            if (sr.item.gw_p10 != null && sr.item.gw_p90 != null)
              rows.push(["P10–P90", fmt1(sr.item.gw_p10) + "–" + fmt1(sr.item.gw_p90)]);
          }
        } else if (f) {
          markX = X(dnum(f.date)); markY = Y(f.p50);
          const nc = f.segment === "nowcast";
          rows.push([shortDate(dnum(f.date)) + (nc ? " · nowcast" : ""), ""]);
          rows.push([nc ? "Est. level" : "Median", fmt1(f.p50) + " mAOD"]);
          rows.push(["P10–P90", fmt1(f.p10) + "–" + fmt1(f.p90)]);
          if (ctx.thr != null)
            rows.push(["vs threshold", (f.p50 - ctx.thr >= 0 ? "+" : "") + fmt1(f.p50 - ctx.thr) + " m"]);
        }
      } else if (ctx.obs.length) {
        const r = nearest(ctx.obs, t, (p) => dnum(p[0]));
        const o = r.item;
        if (o) {
          markX = X(dnum(o[0])); markY = Y(o[1]);
          rows.push([shortDate(dnum(o[0])), ""]);
          rows.push(["Observed", fmt1(o[1]) + " mAOD"]);
        }
      }
      return { markX, markY, rows };
    }

    function move(ev) {
      const ctm = svg.getScreenCTM();
      if (!ctm) return;
      pt.x = ev.clientX; pt.y = ev.clientY;
      const p = pt.matrixTransform(ctm.inverse());
      if (p.x < ctx.m.l || p.x > ctx.m.l + ctx.iw) return restore();
      const r = rowsAt(invX(p.x));
      if (!r.rows.length) return restore();
      draw(r.markX, r.markY, r.rows);
    }

    // Programmatic scrub (the page slider) — pins a marker + returns its rows so
    // the caller can render a DOM readout. Persists until the next scrub.
    function scrubToTime(t) {
      pinnedT = t;
      const r = rowsAt(t);
      if (r.rows.length) draw(r.markX, r.markY, r.rows);
      return r.rows;
    }
    function restore() {
      if (pinnedT != null) {
        const r = rowsAt(pinnedT);
        if (r.rows.length) { draw(r.markX, r.markY, r.rows); return; }
      }
      hide();
    }

    function draw(px, py, rows) {
      const top = ctx.m.t, bot = ctx.m.t + ctx.ih;
      let html = `<line x1="${px.toFixed(1)}" y1="${top}" x2="${px.toFixed(1)}" y2="${bot}" stroke="#33414f" stroke-width="0.6" stroke-opacity="0.5"/>`;
      if (py != null) html += `<circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="3" fill="#fff" stroke="${PAL.above}" stroke-width="1.5"/>`;
      // tooltip box (flip side near the right edge)
      const bw = 96, bh = 12 + rows.length * 11;
      const bx = px + bw + 6 > ctx.m.l + ctx.iw ? px - bw - 6 : px + 6;
      const by = Math.max(top, Math.min(top + 4, bot - bh));
      html += `<rect x="${bx.toFixed(1)}" y="${by.toFixed(1)}" width="${bw}" height="${bh}" rx="3" fill="#fff" stroke="#d8dce1" opacity="0.96"/>`;
      rows.forEach((r, i) => {
        const ty = by + 11 + i * 11;
        if (r[1]) {
          html += `<text x="${(bx + 5).toFixed(1)}" y="${ty}" font-size="8.5" fill="#6b7280">${escAttr(r[0])}</text>`;
          html += `<text x="${(bx + bw - 5).toFixed(1)}" y="${ty}" font-size="8.5" text-anchor="end" fill="#2b3138" font-weight="600">${escAttr(r[1])}</text>`;
        } else {
          html += `<text x="${(bx + 5).toFixed(1)}" y="${ty}" font-size="8.5" fill="#1a3a5c" font-weight="700">${escAttr(r[0])}</text>`;
        }
      });
      layer.innerHTML = html;
    }
    function hide() { layer.innerHTML = ""; }

    svg.addEventListener("mousemove", move);
    svg.addEventListener("mouseleave", restore);
    svg.style.cursor = "crosshair";
    return { scrubToTime, ctx, hide, rowsAt };
  }

  function escAttr(s) {
    return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }
  function monthLabel(s) {
    return s.month_start
      ? new Date(s.month_start + "T00:00:00Z").toLocaleDateString(
          "en-GB", { month: "short", year: "2-digit", timeZone: "UTC" })
      : "M" + s.month_ahead;
  }
  function shortDate(t) {
    const d = new Date(t);
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  }
  function axisDate(t) {
    const d = new Date(t);
    return d.toLocaleDateString("en-GB", { month: "short", year: "2-digit", timeZone: "UTC" });
  }

  // ==========================================================================
  // Ladder: current level on the month's p10/t1/median/t2/p90 quantile rungs.
  // ==========================================================================
  function ladder(level, qrow, statusKey) {
    if (!qrow || qrow.p10 == null) return "";
    const W = 340, H = 52, m = { l: 8, r: 8, t: 16, b: 16 };
    const iw = W - m.l - m.r, y = m.t + 6;
    const lo = qrow.p10, hi = qrow.p90;
    const span = (hi - lo) || 1;
    // clamp the marker into the drawn range
    const X = (v) => m.l + Math.max(0, Math.min(1, (v - lo) / span)) * iw;
    const col = PAL[statusKey] || PAL.none;
    const rungs = [
      ["p10", qrow.p10], ["t1", qrow.t1], ["med", qrow.median],
      ["t2", qrow.t2], ["p90", qrow.p90],
    ];
    const parts = [];
    parts.push(`<line x1="${m.l}" y1="${y}" x2="${m.l + iw}" y2="${y}" stroke="#cfd5db" stroke-width="2"/>`);
    rungs.forEach(([lab, v]) => {
      const x = X(v);
      parts.push(`<line x1="${x.toFixed(1)}" y1="${y - 4}" x2="${x.toFixed(1)}" y2="${y + 4}" stroke="#aab2bb"/>`);
      parts.push(`<text x="${x.toFixed(1)}" y="${y + 15}" text-anchor="middle" font-size="8" fill="#8a929b">${lab}</text>`);
    });
    if (level != null) {
      const x = X(level);
      parts.push(`<circle cx="${x.toFixed(1)}" cy="${y}" r="5" fill="${col}" stroke="#fff" stroke-width="1.5"/>`);
      parts.push(`<text x="${x.toFixed(1)}" y="${y - 8}" text-anchor="middle" font-size="9" font-weight="700" fill="${col}">now</text>`);
    }
    return `<svg class="svg-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Current level against the month's normal range">${parts.join("")}</svg>`;
  }

  // ==========================================================================
  // Seasonal tercile bars — returns HTML (not SVG) for crisp text labels.
  // ==========================================================================
  function seasonalBars(months) {
    if (!months || !months.length) return "";
    // Skip months with no probabilities at all — `100 - 0 - 0` below would
    // otherwise render a null month as a fabricated full "above normal" bar.
    months = months.filter((mo) =>
      mo.p_below != null || mo.p_near != null || mo.p_above != null);
    if (!months.length) return "";
    const rows = months.map((mo) => {
      const b = Math.round((mo.p_below || 0) * 100);
      const n = Math.round((mo.p_near || 0) * 100);
      const a = Math.max(0, 100 - b - n);
      const label = mo.month_start
        ? new Date(mo.month_start + "T00:00:00Z").toLocaleDateString(
            "en-GB", { month: "short", year: "2-digit", timeZone: "UTC" })
        : "M" + mo.month_ahead;
      return `<div class="season-row">
        <span class="mlabel">${esc(label)}</span>
        <div class="season-bar" title="below ${b}% · near ${n}% · above ${a}%">
          <span class="below" style="width:${b}%"></span>
          <span class="near" style="width:${n}%"></span>
          <span class="above" style="width:${a}%"></span>
        </div></div>`;
    });
    return rows.join("");
  }

  // ==========================================================================
  // Verification chart — "how did the last forecast do?". Overlays the ARCHIVED
  // fan (what we published, verbatim) with what was then observed, over the
  // closed window plus a little run-up context. Static (no hover): the summary
  // sentence beside it carries the numbers.
  // ==========================================================================
  function verifyChart(detail, opts) {
    opts = opts || {};
    const v = detail.verification;
    if (!v || !v.fan || !v.fan.length) return "";
    const large = !!opts.large;
    const W = large ? 760 : 340, H = large ? 240 : 160;
    const m = large ? { l: 48, r: 14, t: 14, b: 26 } : { l: 38, r: 8, t: 10, b: 20 };
    const FS = large ? 10.5 : 8.5;
    const iw = W - m.l - m.r, ih = H - m.t - m.b;

    const fan = v.fan.filter((f) => f.p50 != null);
    if (!fan.length) return "";
    const t0 = dnum(fan[0].date), t1 = dnum(fan[fan.length - 1].date);
    const ctx0 = t0 - 5 * 86400000;                       // 5 days of run-up
    const obs = ((detail.observed && detail.observed.series) || [])
      .filter((p) => { const t = dnum(p[0]); return t >= ctx0 && t <= t1; });

    const ys = [];
    fan.forEach((f) => { ys.push(f.p10, f.p50, f.p90); });
    obs.forEach((p) => ys.push(p[1]));
    const [lo, hi] = yRange(ys);
    const X = (t) => m.l + ((t - ctx0) / (t1 - ctx0)) * iw;
    const Y = (val) => m.t + (1 - (val - lo) / (hi - lo)) * ih;

    const parts = [];
    // y gridlines + labels (3 ticks)
    for (let i = 0; i <= 2; i++) {
      const val = lo + ((hi - lo) * i) / 2, y = Y(val);
      parts.push(`<line x1="${m.l}" y1="${y.toFixed(1)}" x2="${W - m.r}" y2="${y.toFixed(1)}" stroke="#eef1f4"/>`);
      parts.push(`<text x="${m.l - 4}" y="${(y + 3).toFixed(1)}" text-anchor="end" font-size="${FS}" fill="#8a8f98">${fmt1(val)}</text>`);
    }
    // archived band + P50 (the published fan, ghosted)
    const top = fan.map((f) => `${X(dnum(f.date)).toFixed(1)},${Y(f.p90).toFixed(1)}`);
    const bot = fan.slice().reverse().map((f) => `${X(dnum(f.date)).toFixed(1)},${Y(f.p10).toFixed(1)}`);
    parts.push(`<polygon points="${top.concat(bot).join(" ")}" fill="${PAL.above}" fill-opacity="0.14" stroke="none"/>`);
    const p50 = fan.map((f) => `${X(dnum(f.date)).toFixed(1)},${Y(f.p50).toFixed(1)}`).join(" ");
    parts.push(`<polyline points="${p50}" fill="none" stroke="${PAL.above}" stroke-width="1.4" stroke-dasharray="4 3" stroke-opacity="0.85"/>`);
    // origin marker
    parts.push(`<line x1="${X(t0).toFixed(1)}" y1="${m.t}" x2="${X(t0).toFixed(1)}" y2="${H - m.b}" stroke="#c3c9d1" stroke-width="1" stroke-dasharray="2 3"/>`);
    parts.push(`<text x="${X(t0).toFixed(1)}" y="${H - m.b + (large ? 16 : 12)}" text-anchor="middle" font-size="${FS}" fill="#8a8f98">issued</text>`);
    // observed: dark line + dots, in/out of band coloured honestly
    if (obs.length) {
      const ol = obs.map((p) => `${X(dnum(p[0])).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ");
      parts.push(`<polyline points="${ol}" fill="none" stroke="#2b3138" stroke-width="1.6"/>`);
      const byDate = {};
      fan.forEach((f) => { byDate[f.date] = f; });
      obs.forEach((p) => {
        const f = byDate[p[0]];
        if (!f) return;                                    // run-up context dot: skip
        const inBand = p[1] >= f.p10 && p[1] <= f.p90;
        parts.push(`<circle cx="${X(dnum(p[0])).toFixed(1)}" cy="${Y(p[1]).toFixed(1)}" r="${large ? 3 : 2.3}" fill="${inBand ? "#2b3138" : "#c0392b"}" stroke="#fff" stroke-width="0.8"/>`);
      });
    }
    // x labels: window start/end
    const dlab = (t) => new Date(t).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
    parts.push(`<text x="${m.l}" y="${H - 6}" font-size="${FS}" fill="#8a8f98">${esc(dlab(ctx0))}</text>`);
    parts.push(`<text x="${W - m.r}" y="${H - 6}" text-anchor="end" font-size="${FS}" fill="#8a8f98">${esc(dlab(t1))}</text>`);

    return `<svg viewBox="0 0 ${W} ${H}" xmlns="${SVGNS}" role="img" ` +
      `aria-label="Archived forecast band with the observations that followed">` +
      parts.join("") + `</svg>`;
  }

  window.GWC_CHARTS = { fanChart, attachFanHover, ladder, seasonalBars, verifyChart };
})();
