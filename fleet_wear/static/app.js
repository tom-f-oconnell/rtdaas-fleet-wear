"use strict";

const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Arms keep their colour whichever robot is shown.
const ARM_ORDER = ["PSM1", "PSM2", "PSM3", "ECM", "MTML", "MTMR"];
const armColor = (arm) => css(`--series-${ARM_ORDER.indexOf(arm) % 6 + 1}`);
const NOT_LOAD = new Set(["travel", "reversals"]);
const INDEX_SPAN = 2; // the heat map runs from a quarter of the fleet's median to four times it

let fleet = null;
let robot = null;

async function getJson(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || body.error || `HTTP ${response.status}`);
  return body;
}

function fmt(value, digits = 3) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "–";
  if (value !== 0 && (Math.abs(value) >= 1e5 || Math.abs(value) < 1e-3)) return value.toExponential(2);
  return Number(value.toPrecision(digits)).toLocaleString();
}

function valueUnit(metric, cell) {
  if (metric === "travel") return cell.unit;
  if (metric === "reversals") return "";
  if (metric === "work") return "J";
  return cell.unit === "m" ? "N³·m" : "(N·m)³·rad";
}

function drawHeatmap(metric) {
  const robots = [...new Set(fleet.cells.map((c) => `${c.robot_id} ${c.arm}`))].sort();
  const joints = [...new Set(fleet.cells.map((c) => c.joint))];
  const byKey = new Map(fleet.cells.map((c) => [`${c.robot_id} ${c.arm}|${c.joint}`, c]));
  const z = [];
  const text = [];
  robots.forEach((row) => {
    const zRow = [];
    const textRow = [];
    joints.forEach((joint) => {
      const cell = byKey.get(`${row}|${joint}`);
      const index = cell ? cell[`${metric}_index`] : null;
      zRow.push(index === null || index === undefined || index <= 0 ? null : Math.max(-INDEX_SPAN, Math.min(INDEX_SPAN, Math.log2(index))));
      textRow.push(
        cell
          ? `${esc(row)} / ${esc(joint)}<br>${fmt(cell[metric])} ${esc(valueUnit(metric, cell))}<br>${index ? `${fmt(index, 2)} × fleet median` : cell.comparable ? "no load data" : "moved too little to compare"}`
          : "no data",
      );
    });
    z.push(zRow);
    text.push(textRow);
  });
  const trace = {
    type: "heatmap",
    x: joints,
    y: robots,
    z,
    text,
    hoverinfo: "text",
    zmin: -INDEX_SPAN,
    zmax: INDEX_SPAN,
    xgap: 2,
    ygap: 2,
    colorscale: [[0, css("--low")], [0.5, css("--mid")], [1, css("--high")]],
    colorbar: {
      title: { text: "Against the fleet's median for that joint", side: "right" },
      tickvals: [-2, -1, 0, 1, 2],
      ticktext: ["¼ ×", "½ ×", "median", "2 ×", "4 ×"],
      thickness: 12,
    },
  };
  Plotly.react(
    "heatmap",
    [trace],
    {
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: css("--missing"),
      font: { family: "system-ui, -apple-system, 'Segoe UI', sans-serif", size: 12, color: css("--ink-2") },
      height: Math.max(280, 34 * robots.length + 70),
      margin: { l: 90, r: 20, t: 8, b: 50 },
      yaxis: { autorange: "reversed", tickfont: { size: 12 } },
      xaxis: { tickfont: { size: 12 } },
    },
    { responsive: true, displayModeBar: false },
  );
}

function tableHtml(metric) {
  const key = `${metric}_index`;
  const rows = [...fleet.cells]
    .sort((a, b) => (b[key] ?? -1) - (a[key] ?? -1))
    .map(
      (c) => `<tr><td>${esc(c.robot_id)}</td><td>${esc(c.arm)}</td><td>${esc(c.joint)}</td>
        <td class="num">${fmt(c[key], 2)}</td><td class="num">${fmt(c.travel)} ${esc(c.unit)}</td>
        <td class="num">${fmt(c.reversals)}</td><td class="num">${fmt(c.work)}</td><td class="num">${fmt(c.damage)}</td>
        <td class="num">${fmt(c.equivalent_load)}</td><td class="num">${c.load_coverage === null ? "–" : `${Math.round(c.load_coverage * 100)} %`}</td>
        ${fleet.has_ratings ? `<td class="num">${c.life_used === null ? "–" : `${fmt(c.life_used * 100, 2)} %`}</td>` : ""}</tr>`,
    )
    .join("");
  return `<table><thead><tr><th>Robot</th><th>Arm</th><th>Joint</th><th class="num">Against fleet</th><th class="num">Movement</th>
    <th class="num">Direction changes</th><th class="num">Load × movement</th><th class="num">Damage</th><th class="num">Equivalent load</th><th class="num">Load data for</th>
    ${fleet.has_ratings ? `<th class="num">Life used</th>` : ""}</tr></thead><tbody>${rows}</tbody></table>`;
}

function tilesHtml(metric) {
  const s = fleet.summary;
  const key = `${metric}_index`;
  const ranked = fleet.cells.filter((c) => c[key] !== null).sort((a, b) => b[key] - a[key]);
  const top = ranked[0];
  const coverage = s.load_coverage === null ? "–" : `${Math.round(s.load_coverage * 100)} %`;
  return `
    <div class="tile"><div class="value">${s.robots}</div><div class="label">Robots</div><div class="sub">${s.arms} arms</div></div>
    <div class="tile"><div class="value">${fmt(s.observed_hours, 3)} h</div><div class="label">Recorded operation</div><div class="sub">${s.gaps_per_robot ? `Up to ${s.gaps_per_robot} breaks per robot; movement in them is unknown` : "No breaks in the data"}</div></div>
    <div class="tile"><div class="value">${coverage}</div><div class="label">Movement with usable load data</div><div class="sub">Load measures use only this part</div></div>
    <div class="tile"><div class="value">${top ? `${fmt(top[key], 2)} ×` : "–"}</div><div class="label">Furthest above the fleet</div><div class="sub">${top ? `${esc(top.robot_id)} ${esc(top.arm)} ${esc(top.joint)}` : "No load data"}</div></div>`;
}

async function drawHistory(metric) {
  const data = await getJson(`/app/history?${new URLSearchParams({ robot_id: robot, metric })}`);
  const joints = Object.keys(data.series);
  const arms = ARM_ORDER.filter((a) => joints.some((j) => data.series[j][a]));
  const traces = [];
  joints.forEach((joint, i) => {
    arms.forEach((arm) => {
      const line = data.series[joint][arm];
      if (!line) return;
      traces.push({
        type: "scatter",
        mode: "lines+markers",
        name: arm,
        legendgroup: arm,
        showlegend: i === 0,
        x: line.t,
        y: line.y,
        xaxis: i === 0 ? "x" : `x${i + 1}`,
        yaxis: i === 0 ? "y" : `y${i + 1}`,
        line: { color: armColor(arm), width: 2 },
        marker: { size: 8, color: armColor(arm), line: { color: css("--surface"), width: 2 } },
        hovertemplate: `%{y:.4g}<extra>${arm}</extra>`,
      });
    });
  });
  const columns = 3;
  const rows = Math.ceil(joints.length / columns);
  const layout = {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: "system-ui, -apple-system, 'Segoe UI', sans-serif", size: 12, color: css("--ink-2") },
    height: 260 * rows + 70,
    margin: { l: 56, r: 14, t: 70, b: 36 },
    hovermode: "x unified",
    legend: { orientation: "h", x: 0, y: 1.1, yanchor: "bottom" },
    grid: { rows, columns, pattern: "independent", xgap: 0.1, ygap: 0.28 },
    annotations: joints.map((joint, i) => ({
      text: `<b>${esc(joint)}</b> (${metric === "travel" ? esc(data.units[joint]) : metric === "reversals" ? "count" : metric === "work" ? "J" : "damage"})`,
      xref: `x${i === 0 ? "" : i + 1} domain`,
      yref: `y${i === 0 ? "" : i + 1} domain`,
      x: 0,
      y: 1.04,
      xanchor: "left",
      yanchor: "bottom",
      showarrow: false,
      font: { size: 12, color: css("--ink") },
    })),
  };
  joints.forEach((_, i) => {
    const suffix = i === 0 ? "" : i + 1;
    layout[`xaxis${suffix}`] = { gridcolor: css("--grid"), linecolor: css("--axis"), nticks: 5, tickformat: "%b %d" };
    layout[`yaxis${suffix}`] = { gridcolor: css("--grid"), linecolor: css("--axis"), rangemode: "tozero" };
  });
  Plotly.react("history", traces, layout, { responsive: true, displayModeBar: false });
}

function render() {
  const metric = $("metric").value;
  $("explain").textContent = fleet.metrics[metric].explain;
  if (!fleet.summary) {
    $("tiles").innerHTML = "";
    $("main").innerHTML = `<div class="card">Nothing recorded yet. Use “Read new data”, or wait for the next automatic read.</div>`;
    return;
  }
  const s = fleet.summary;
  const notices = [];
  if (s.noise_share > 0.01) notices.push(`${Math.round(s.noise_share * 100)} % of movement has effort readings that look like noise, not load, so load measures skip it.`);
  if (s.absent_share > 0.01) notices.push(`${Math.round(s.absent_share * 100)} % of movement has no effort readings.`);
  $("notice").textContent = notices.join(" ");
  $("tiles").innerHTML = tilesHtml(metric);
  const robots = [...new Set(fleet.cells.map((c) => c.robot_id))].sort();
  if (!robots.includes(robot)) robot = robots[0];
  $("main").className = "";
  $("main").innerHTML = `
    <div class="card"><h2>${esc(fleet.metrics[metric].label)}, every joint against its peers</h2>
      <p class="note">Each joint is compared with the same joint on arms of the same kind. Red has done more than its peers, blue less. Grey squares have no data for this measure, or moved less than a full turn (10 cm for insertion), too little to compare.</p>
      <div id="heatmap" class="plot"></div></div>
    <div class="card"><div class="card-head"><h2>${esc(fleet.metrics[metric].label)} over time</h2>
      <label>Robot <select id="robot">${robots.map((r) => `<option ${r === robot ? "selected" : ""}>${esc(r)}</option>`).join("")}</select></label></div>
      <div id="history" class="plot"></div></div>
    <div class="card"><details><summary>Every joint, as a table</summary><div class="scroll">${tableHtml(metric)}</div></details></div>`;
  $("robot").addEventListener("change", () => {
    robot = $("robot").value;
    if (typeof Plotly !== "undefined") drawHistory(metric).catch(showError);
  });
  if (typeof Plotly === "undefined") {
    for (const id of ["heatmap", "history"]) {
      $(id).innerHTML = `<p class="muted">Charts need Plotly from cdn.plot.ly, which did not load. The table below still works.</p>`;
    }
    return;
  }
  drawHeatmap(metric);
  drawHistory(metric).catch(showError);
}

function showError(error) {
  $("main").className = "";
  $("main").innerHTML = `<div class="card error"><h2>Something went wrong</h2><p>${esc(error.message)}</p></div>`;
}

async function refreshStatus() {
  const s = await getJson("/app/status");
  $("sync").disabled = s.running;
  $("sync").textContent = s.running ? "Reading…" : "Read new data";
  const last = s.last_finished ? `last read ${s.last_finished.slice(0, 19).replace("T", " ")} UTC` : "not read yet this run";
  $("status").textContent = s.last_error ? `Could not read ${s.api_url}: ${s.last_error}` : `Data from ${s.api_url}, ${last}`;
  return s;
}

async function load() {
  fleet = await getJson("/app/fleet");
  render();
}

async function start() {
  const initial = await getJson("/app/fleet");
  fleet = initial;
  $("metric").innerHTML = Object.entries(fleet.metrics).map(([key, m]) => `<option value="${key}">${esc(m.label)}</option>`).join("");
  $("metric").value = new URLSearchParams(location.hash.slice(1)).get("metric") || "damage";
  $("metric").addEventListener("change", render);
  $("sync").addEventListener("click", async () => {
    await getJson("/app/sync", { method: "POST" });
    await refreshStatus();
    const timer = setInterval(async () => {
      const s = await refreshStatus();
      if (!s.running) {
        clearInterval(timer);
        await load();
      }
    }, 2000);
  });
  await refreshStatus();
  render();
}

start().catch(showError);
