(function () {
  "use strict";
  const data = window.TRACKER_DATA;
  if (!data || !Array.isArray(data.sources)) {
    document.body.innerHTML = '<p class="empty">The tracker snapshot could not be loaded.</p>';
    return;
  }

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[c]);
  const slug = (value) => String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  const niceDate = (value) => {
    if (!value) return "Not recorded";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString(undefined, {year:"numeric",month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",timeZone:"UTC",timeZoneName:"short"});
  };
  const pill = (value) => `<span class="pill ${slug(value)}">${esc(value || "—")}</span>`;
  const state = { page: 1, pageSize: 100, sourceSort: ["position", 1], countrySort: ["country", 1], filtered: [...data.sources], mapCountry: "", detailTrigger: null };
  const sharedCountryCodes = new Set(["INT", "REG"]);
  const geometry = Array.isArray(window.WORLD_GEOMETRY) ? window.WORLD_GEOMETRY : [];
  const geometryByCode = new Map(geometry.map(feature => [feature.id, feature]));
  const countryByCode = new Map(data.countries.filter(row => !sharedCountryCodes.has(row.country)).map(row => [row.country, row]));
  const sourceRowsByCountry = new Map(data.countries.map(country => [country.country, data.sources.filter(row => row.country === country.country)]));

  const sourcePositionOrder = [
    "Blocked",
    "Inspected; verification pending",
    "Verified through earlier boundary",
    "Not inspected",
    "Complete through target",
  ];

  function sourcePosition(row) {
    const value = String(row.first_pass_state || "").trim();
    return sourcePositionOrder.includes(value) ? value : "Not inspected";
  }

  function sourcePositionRank(row) {
    return sourcePositionOrder.indexOf(sourcePosition(row));
  }

  function sourcePlaceLabel(row) {
    if (row.country === "INT") return "International";
    if (row.country === "REG") return "Regional";
    return countryName(row.country);
  }

  function injectSourceDirectoryStyles() {
    if ($("#source-directory-styles")) return;
    const style = document.createElement("style");
    style.id = "source-directory-styles";
    style.textContent = `
      .source-position-band{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin:0 0 18px}
      .position-tile{appearance:none;text-align:left;border:1px solid var(--line);border-top:4px solid #a8a49d;background:var(--card);border-radius:14px;padding:14px 15px;color:var(--ink);box-shadow:0 7px 20px rgba(31,45,53,.04);min-height:94px}
      .position-tile:hover,.position-tile.active{border-color:var(--teal);transform:translateY(-1px)}
      .position-tile strong{display:block;font-family:Georgia,serif;font-size:1.65rem;font-weight:500;line-height:1;margin-bottom:7px}
      .position-tile span{display:block;color:var(--muted);font-size:.72rem;font-weight:720;line-height:1.25}
      .position-tile[data-position="Complete through target"]{border-top-color:#21836f}.position-tile[data-position="Inspected; verification pending"]{border-top-color:#d49a32}.position-tile[data-position="Verified through earlier boundary"]{border-top-color:#4b7eaa}.position-tile[data-position="Blocked"]{border-top-color:#c85143}.position-tile[data-position="Not inspected"]{border-top-color:#9e9a93}
      .source-directory-intro{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin:0 0 12px}
      .source-directory-intro h2{font-family:Georgia,serif;font-weight:500;margin:3px 0 5px;font-size:1.55rem}.source-directory-intro p{margin:0;color:var(--muted);font-size:.83rem;line-height:1.5}
      .source-primary{min-width:230px}.source-primary strong{display:block;color:var(--navy);font-size:.86rem}.source-primary small{display:block;color:var(--muted);font-size:.67rem;margin-top:3px}
      .boundary-cell{min-width:122px}.boundary-cell strong{display:block;color:#344851;font-size:.76rem}.boundary-cell small{display:block;color:var(--muted);font-size:.64rem;margin-top:3px}
      .action-cell{min-width:230px;max-width:310px}.action-cell .pill{margin-bottom:5px}.action-cell p{margin:0;color:#56636c;font-size:.73rem;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
      .source-table-wrap{max-height:68vh}.source-table-wrap th{z-index:2}.source-table-wrap td:first-child,.source-table-wrap th:first-child{position:sticky;left:0;z-index:1;background:var(--card)}.source-table-wrap th:first-child{z-index:3;background:#f7f5f0}.source-table-wrap tbody tr:hover td:first-child{background:#f5faf8}
      .pill.complete-through-target{background:#dcefe9;color:#17685e}.pill.inspected-verification-pending{background:#f7e9c9;color:#845d16}.pill.verified-through-earlier-boundary{background:#dfeaf4;color:#315f85}.pill.not-inspected{background:#ece9e3;color:#66615a}
      .page-size-label{display:flex;align-items:center;gap:7px}.page-size-label span{font-weight:700}.page-size-label select{padding:6px 28px 6px 8px}
      .detail-backdrop{display:none;position:fixed;inset:0;background:rgba(15,29,36,.42);z-index:80}.detail-backdrop.open{display:block}
      .detail-drawer{display:block;position:fixed;z-index:90;right:0;top:0;bottom:0;width:min(610px,92vw);margin:0;border:0;border-left:1px solid var(--line);border-radius:0;padding:28px 30px 40px;overflow:auto;transform:translateX(102%);transition:transform .22s ease;box-shadow:-22px 0 60px rgba(18,34,42,.18)}.detail-drawer.open{display:block;transform:translateX(0)}
      .detail-head{position:sticky;top:-28px;background:var(--card);z-index:2;padding:28px 0 16px;border-bottom:1px solid var(--line)}.detail-head h3{padding-right:16px}.detail-status{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}.close-detail{font-size:1.65rem;line-height:1;padding:4px 8px;border-radius:8px}.close-detail:hover{background:#f0ede7}
      .detail-section-title{font-size:.7rem;text-transform:uppercase;letter-spacing:.09em;color:var(--teal);margin:22px 0 10px;font-weight:800}.coverage-track{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.coverage-step{border:1px solid var(--line);border-radius:12px;padding:12px;background:#faf8f4}.coverage-step span{display:block;color:var(--muted);font-size:.63rem;text-transform:uppercase;font-weight:780;margin-bottom:5px}.coverage-step strong{font-size:.84rem;color:#30444d}.processing-track{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.processing-step{padding:11px;border-left:3px solid var(--mint);background:#f5f8f6;border-radius:0 10px 10px 0}.processing-step span{display:block;color:var(--muted);font-size:.62rem;text-transform:uppercase;font-weight:780}.processing-step strong{display:block;margin-top:4px;font-size:.78rem}
      .source-row-hint{color:var(--muted);font-size:.68rem;margin-top:4px}.source-table-wrap tbody tr:focus{outline:3px solid rgba(22,127,122,.26);outline-offset:-3px;background:#f5faf8}
      @media(max-width:1100px){.source-position-band{grid-template-columns:repeat(3,1fr)}.source-table-wrap{max-height:none}}
      @media(max-width:720px){.source-position-band{grid-template-columns:repeat(2,1fr)}.position-tile{min-height:82px}.source-directory-intro{align-items:stretch;flex-direction:column}.detail-drawer{width:100vw;padding:22px 20px 32px}.detail-head{top:-22px;padding-top:22px}.coverage-track{grid-template-columns:1fr}.processing-track{grid-template-columns:1fr}.detail-grid,.detail-copy{grid-template-columns:1fr}.source-table-wrap td:nth-child(4),.source-table-wrap th:nth-child(4),.source-table-wrap td:nth-child(7),.source-table-wrap th:nth-child(7),.source-table-wrap td:nth-child(8),.source-table-wrap th:nth-child(8){display:none}}
    `;
    document.head.append(style);
  }

  function prepareSourceDirectory() {
    injectSourceDirectoryStyles();
    const sourceButton = $('.tab[data-tab="sources"]');
    const tabs = $(".tabs");
    if (sourceButton && tabs) tabs.insertBefore(sourceButton, tabs.firstElementChild);
    $$(".tab").forEach(button => button.classList.toggle("active", button.dataset.tab === "sources"));
    $$(".tab-panel").forEach(panel => panel.classList.toggle("active", panel.id === "tab-sources"));
    $(".hero h1").textContent = "See where every source stands";
    $(".hero p").textContent = "Search all sources and compare their recorded boundary, operational coverage, receipt-backed verification, processing stage, and next action.";

    const sourcePanel = $("#tab-sources");
    const tableCard = $("#source-table").closest(".table-card");
    const intro = document.createElement("div");
    intro.className = "source-directory-intro";
    intro.innerHTML = `<div><span class="section-kicker">Source directory</span><h2>Every source, one clear position</h2><p>Choose a status to narrow the directory. Select any row for its full coverage and processing record.</p></div><button id="show-all-sources" class="secondary-button">Show all sources</button>`;
    const band = document.createElement("div");
    band.id = "source-position-band";
    band.className = "source-position-band";
    band.setAttribute("aria-label", "Filter sources by current position");
    sourcePanel.insertBefore(band, tableCard);
    sourcePanel.insertBefore(intro, band);

    const title = $("#tab-sources .table-title h2");
    if (title) title.textContent = "Source directory";
    const firstPassLabel = $("#filter-first-pass")?.closest("label")?.querySelector("span");
    if (firstPassLabel) firstPassLabel.textContent = "Where it stands";
    const firstPassDefault = $("#filter-first-pass option");
    if (firstPassDefault) firstPassDefault.textContent = "All positions";

    $("#source-table thead tr").innerHTML = `
      <th data-sort="source">Source</th>
      <th data-sort="position">Where it stands</th>
      <th data-sort="recorded_notion_boundary">Recorded Notion</th>
      <th data-sort="recorded_through">Operational through</th>
      <th data-sort="first_pass_verified_through">First-pass verified</th>
      <th data-sort="verified_through">Strict verified</th>
      <th data-sort="pipeline_state">Current stage</th>
      <th data-sort="attention_state">Attention and next action</th>
      <th data-sort="last_checked">Last reviewed</th>`;

    const pagination = $("#tab-sources .pagination");
    const sizeLabel = document.createElement("label");
    sizeLabel.className = "page-size-label";
    sizeLabel.innerHTML = '<span>Rows</span><select id="source-page-size" aria-label="Rows per page"><option value="50">50</option><option value="100" selected>100</option><option value="all">All</option></select>';
    pagination.insertBefore(sizeLabel, pagination.firstElementChild);

    const drawer = $("#source-detail");
    drawer.setAttribute("role", "dialog");
    drawer.setAttribute("aria-modal", "true");
    drawer.setAttribute("aria-label", "Source details");
    drawer.setAttribute("aria-hidden", "true");
    const backdrop = document.createElement("div");
    backdrop.id = "detail-backdrop";
    backdrop.className = "detail-backdrop";
    document.body.append(backdrop);
  }

  function isoDate(value) {
    const match = String(value || "").match(/^\d{4}-\d{2}-\d{2}/);
    return match ? match[0] : "";
  }

  function compactMonth(value) {
    const raw = isoDate(value);
    if (!raw) return "Not recorded";
    const parsed = new Date(`${raw}T00:00:00Z`);
    return parsed.toLocaleDateString(undefined, {year:"numeric", month:"short", timeZone:"UTC"});
  }

  function monthRange(from, through) {
    const start = isoDate(from), end = isoDate(through);
    if (!end) return "No target month";
    if (!start || start.slice(0, 7) === end.slice(0, 7)) return compactMonth(end);
    const startDate = new Date(`${start}T00:00:00Z`), endDate = new Date(`${end}T00:00:00Z`);
    const sameYear = startDate.getUTCFullYear() === endDate.getUTCFullYear();
    const startLabel = startDate.toLocaleDateString(undefined, {month:"short", ...(sameYear ? {} : {year:"numeric"}), timeZone:"UTC"});
    const endLabel = endDate.toLocaleDateString(undefined, {month:"short", year:"numeric", timeZone:"UTC"});
    return `${startLabel}\u2013${endLabel}`;
  }

  function dateAfterSafeMonth(safeDate, through) {
    const safe = isoDate(safeDate), end = isoDate(through);
    if (!safe || !end || safe >= end) return "";
    if (safe.slice(0, 7) === end.slice(0, 7)) return end;
    const parsed = new Date(`${safe}T00:00:00Z`);
    parsed.setUTCMonth(parsed.getUTCMonth() + 1, 1);
    return parsed.toISOString().slice(0, 10);
  }

  function countryCoverageContext(country) {
    const rows = sourceRowsByCountry.get(country.country) || [];
    const total = Number(country.sources || rows.length || 0);
    const safeDate = isoDate(country.safe_through || country.strict_safe_through);
    const explicitThrough = isoDate(country.cycle_coverage_through || country.later_coverage_through || country.later_through || country.majority_through);
    const explicitFrom = isoDate(country.cycle_coverage_from || country.later_coverage_from || country.later_from) || dateAfterSafeMonth(safeDate, explicitThrough);
    const explicitCount = Number(country.cycle_covered_sources ?? country.cycle_coverage_sources ?? country.later_coverage_sources ?? country.later_covered ?? country.majority_covered);
    const explicitPct = Number(country.cycle_coverage_pct ?? country.later_coverage_pct ?? country.later_pct ?? country.majority_pct);

    let through = explicitThrough;
    let from = explicitFrom;
    let count = Number.isFinite(explicitCount) ? explicitCount : NaN;
    let pct = Number.isFinite(explicitPct) ? explicitPct : NaN;

    if (!through || !Number.isFinite(count)) {
      through = isoDate(country.coverage_target || data.meta.baseline_end);
      from = dateAfterSafeMonth(safeDate, through);
      count = rows.filter(row => row.baseline_status === "Done" && sourceCycleDate(row) >= through).length;
      pct = total ? count / total * 100 : 0;
    }

    if (!Number.isFinite(pct)) pct = total ? count / total * 100 : 0;
    const range = monthRange(from, through);
    const operationalState = String(country.cycle_coverage_state || "").trim() || (
      count === total && total ? "complete" : count >= Math.ceil(total * 0.8) ? "mostly covered" : count ? "partial" : "not checked this cycle"
    );
    const safeLabel = safeDate ? compactDate(safeDate) : "Not yet verified";
    const countLabel = `${count}/${total} (${Math.round(pct)}%)`;
    const summary = count === total && total
      ? `All ${total} sources through ${compactMonth(through)}`
      : `${countLabel} through ${range}`;
    return {safeDate, safeLabel, through, from, range, count, total, pct, countLabel, summary, operationalState};
  }

  function countryStageContext(country, prefix) {
    const rows = sourceRowsByCountry.get(country.country) || [];
    const total = Number(country.sources || rows.length || 0);
    const complete = Number(country[`${prefix}_complete`] || 0);
    const explicitParts = ["partial", "active", "waiting", "blocked", "unverified", "unassessed"]
      .map(key => [key, Number(country[`${prefix}_${key}`] || 0)])
      .filter(([, count]) => count > 0);
    const field = prefix === "patchtools" ? "patchtools_status" : "pretcg_status";
    const unknown = rows.filter(row => /^(not assessed|not verified|unknown|)$/i.test(String(row[field] || "").trim())).length;
    if (total && complete === 0 && unknown === total && !explicitParts.length) {
      return {value:"Not checked", note:"No source record"};
    }
    const note = explicitParts.slice(0, 2).map(([key, count]) => `${count} ${key}`).join(" · ");
    return {value:`${complete}/${total}`, note};
  }

  function sourceCycleDate(row) {
    return isoDate(row.scrape_verified_through || row.coverage_through);
  }

  function sourceFirstPassDate(row) {
    return isoDate(row.first_pass_verified_through);
  }

  function countryFirstPassContext(country) {
    const total = Number(country.sources || 0);
    const inspected = Number(country.first_pass_inspected || 0);
    const complete = Number(country.first_pass_complete || 0);
    return {
      total, inspected, complete,
      state: country.first_pass_progress || "Not started",
      target: isoDate(country.first_pass_target || data.meta.first_pass_target_end),
      label: `${inspected}/${total} inspected · ${complete}/${total} complete`,
    };
  }

  function sourceScrapeRecordDate(row) {
    return isoDate(row.scrape_record_date || row.scrape_basic_record_date || row.scrape_record_through || row.legacy_last_update);
  }

  function sourceTcgRecordDate(row) {
    return isoDate(row.tcg_record_date || row.tcg_basic_record_date || row.tcg_record_through);
  }

  function optionalNumber(value) {
    if (value === undefined || value === null || value === "") return NaN;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : NaN;
  }

  function derivedDateCoverage(rows, getDate) {
    const total = rows.length;
    const dates = rows.map(getDate).filter(Boolean);
    const safeDate = dates.length === total && total ? [...dates].sort()[0] : "";
    const frontiers = [...new Set(dates)].sort().reverse();
    const required = Math.ceil(total * 0.8);
    const through = frontiers.find(date => dates.filter(value => value >= date).length >= required) || "";
    const count = through ? dates.filter(value => value >= through).length : 0;
    return {safeDate, through, count, total, recorded:dates.length, pct:total ? count / total * 100 : 0};
  }

  function countryRecordedContext(country, kind) {
    const rows = sourceRowsByCountry.get(country.country) || [];
    const isScrape = kind === "scrape";
    const derived = derivedDateCoverage(rows, isScrape ? sourceScrapeRecordDate : sourceTcgRecordDate);
    const total = Number(country.sources || rows.length || 0);
    const safeDate = isoDate(isScrape
      ? country.recorded_scrape_floor || country.scrape_record_floor || country.scrape_record_safe_through
      : country.recorded_tcg_floor || country.tcg_recorded_floor || country.tcg_safe_through) || derived.safeDate;
    const through = isoDate(isScrape
      ? country.recorded_scrape_majority_through || country.scrape_record_majority_through || country.recorded_scrape_through
      : country.recorded_tcg_majority_through || country.tcg_recorded_through || country.tcg_majority_through || country.tcg_through) || derived.through;
    const explicitCount = optionalNumber(isScrape
      ? country.recorded_scrape_majority_sources ?? country.recorded_scrape_majority_covered ?? country.scrape_record_majority_covered ?? country.recorded_scrape_covered
      : country.recorded_tcg_majority_sources ?? country.tcg_recorded_covered ?? country.tcg_majority_covered);
    const explicitPct = optionalNumber(isScrape
      ? country.recorded_scrape_majority_pct ?? country.scrape_record_majority_pct ?? country.recorded_scrape_pct
      : country.recorded_tcg_majority_pct ?? country.tcg_recorded_pct ?? country.tcg_majority_pct);
    const explicitRecorded = optionalNumber(isScrape
      ? country.recorded_scrape_sources ?? country.scrape_recorded
      : country.recorded_tcg_sources ?? country.tcg_recorded);
    const count = Number.isFinite(explicitCount) ? explicitCount : derived.count;
    const pct = Number.isFinite(explicitPct) ? explicitPct : (total ? count / total * 100 : 0);
    const recorded = Number.isFinite(explicitRecorded) ? explicitRecorded : derived.recorded;
    const safeLabel = safeDate ? compactDate(safeDate) : "No complete recorded floor";
    const throughLabel = through ? compactMonth(through) : "No dated record";
    const countLabel = through ? `${count}/${total} (${Math.round(pct)}%)` : `${recorded}/${total} recorded`;
    return {safeDate, safeLabel, through, throughLabel, count, total, pct, recorded, countLabel};
  }

  function countryTcgContext(country) {
    return countryRecordedContext(country, "tcg");
  }

  function countryOperationalContext(country) {
    return countryRecordedContext(country, "scrape");
  }

  function operationalBoundaryCell(country) {
    const operational = countryOperationalContext(country);
    const detail = operational.through
      ? `confidence: ${operational.countLabel} of sources recorded at or beyond this date`
      : `${operational.recorded}/${operational.total} sources have a dated record`;
    return `<div class="coverage-cell"><strong>${esc(operational.throughLabel)}</strong><small>${esc(detail)}</small></div>`;
  }

  function verifiedBoundaryCell(country) {
    const coverage = countryCoverageContext(country);
    const recorded = Number(country.coverage_recorded);
    const detail = coverage.safeDate
      ? "receipt-backed across all included sources"
      : `${Number.isFinite(recorded) ? recorded : 0}/${country.sources} sources have receipt-grade dates`;
    return `<div class="coverage-cell"><strong>${esc(coverage.safeLabel)}</strong><small>${esc(detail)}</small></div>`;
  }

  function tcgBoundaryCell(country) {
    const tcg = countryTcgContext(country);
    const detail = tcg.through ? `${tcg.countLabel} of sources` : `${tcg.recorded}/${tcg.total} dated records`;
    return `<div class="coverage-cell"><strong>${esc(tcg.throughLabel)}</strong><small>${esc(detail)}</small></div>`;
  }

  function currentProcessingCell(country) {
    const patchtools = countryStageContext(country, "patchtools");
    const pretcg = countryStageContext(country, "pretcg");
    return `<div class="history-summary"><span><b>Patchtools</b> ${esc(patchtools.value)}${patchtools.note ? ` · ${esc(patchtools.note)}` : ""}</span><span><b>Pre-TCG</b> ${esc(pretcg.value)}${pretcg.note ? ` · ${esc(pretcg.note)}` : ""}</span></div>`;
  }

  function activateTab(name) {
    $$(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
    $$(".tab-panel").forEach(p => p.classList.toggle("active", p.id === `tab-${name}`));
    window.scrollTo({top: Math.max(0, $(".tabs").offsetTop - 12), behavior:"smooth"});
  }
  if (window.SOURCE_TRACKER_TEST_MODE) {
    window.SOURCE_TRACKER_TEST_API = {sourcePosition, sourcePositionRank, sourcePositionOrder:[...sourcePositionOrder]};
    return;
  }
  $$(".tab").forEach(button => button.addEventListener("click", () => activateTab(button.dataset.tab)));
  $$('[data-go]').forEach(button => button.addEventListener("click", () => activateTab(button.dataset.go)));

  function renderHeader() {
    $("#snapshot-date").textContent = niceDate(data.meta.generated_at_utc);
    $("#baseline-label").textContent = `First-pass target: ${data.meta.first_pass_target_end || data.meta.baseline_end || "not set"}`;
    const done = data.summary.baseline_status.Done || 0;
    const firstPass = data.summary.first_pass_state || {};
    const firstPassInspected = data.meta.source_count - Number(firstPass["Not inspected"] || 0);
    const firstPassComplete = Number(firstPass["Complete through target"] || 0);
    const active = data.summary.attention_state.Active || 0;
    const blocked = data.summary.attention_state.Blocked || 0;
    const metrics = [
      [data.meta.source_count, "Sources", `${data.meta.international_regional_source_count} shared INT/REG`],
      [data.meta.national_country_count, "Countries", `${data.meta.country_group_count} groups with INT/REG`],
      [firstPassInspected, "First-pass inspected", `${Math.round(firstPassInspected / data.meta.source_count * 100)}% of sources`],
      [firstPassComplete, "Through target", `${data.meta.first_pass_target_end || data.meta.baseline_end}`],
      [active + blocked, "Needs attention", `${active} active · ${blocked} blocked`],
    ];
    $("#metric-cards").innerHTML = metrics.map(([n,label,note]) => `<article class="metric"><span>${esc(label)}</span><strong>${esc(n)}</strong><small>${esc(note)}</small></article>`).join("");
  }

  function renderDonut() {
    const counts = data.summary.baseline_status;
    const palette = {Done:"#167f7a", Open:"#e2aa49", Unassessed:"#d6d1c8"};
    const labels = {Done:"Cycle covered", Open:"Cycle gap", Unassessed:"No current-cycle receipt"};
    const total = Object.values(counts).reduce((a,b) => a+b, 0);
    let cursor = 0;
    const stops = Object.entries(counts).map(([label,count]) => {
      const start = cursor; cursor += count / total * 360;
      return `${palette[label] || "#7167a8"} ${start}deg ${cursor}deg`;
    }).join(",");
    $("#baseline-chart").innerHTML = `<div class="donut" style="background:conic-gradient(${stops})"><div class="donut-label"><strong>${counts.Done || 0}</strong><span>cycle covered</span></div></div><div class="legend">${Object.entries(counts).map(([label,count]) => `<div class="legend-row"><i class="swatch" style="background:${palette[label] || "#7167a8"}"></i><span>${esc(labels[label] || label)}</span><strong>${count}</strong></div>`).join("")}</div>`;
    $("#baseline-total").textContent = `${total} total sources`;
  }

  function renderPipeline() {
    const counts = data.sources.reduce((acc, row) => {
      const stage = displayPipelineStage(row);
      acc[stage] = (acc[stage] || 0) + 1;
      return acc;
    }, {});
    const entries = Object.entries(counts).sort((a,b) => b[1]-a[1]);
    const max = Math.max(...entries.map(e=>e[1]),1);
    $("#pipeline-chart").innerHTML = entries.map(([label,count]) => `<div class="bar-row"><span class="bar-label" title="${esc(label)}">${esc(label)}</span><div class="bar-track"><div class="bar-fill" style="width:${count/max*100}%"></div></div><strong class="bar-value">${count}</strong></div>`).join("");
  }

  function displayPipelineStage(row) {
    const current = String(row.pipeline_state || "").trim();
    if (current && !/^unassessed$/i.test(current)) return current;
    const legacy = String(row.legacy_stage || row.stage || "").trim().toLowerCase();
    const labels = {
      counts_generated:"TCG recorded",
      complete:"Pipeline complete",
      postprocessing:"Postprocessing recorded",
      scraped:"Scrape recorded",
      running:"Active",
      queued:"Queued",
    };
    return labels[legacy] || "No pipeline record";
  }

  function cycleStatusLabel(row) {
    if (row.cycle_coverage_status) return row.cycle_coverage_status;
    return {Done:"Cycle covered", Open:"Cycle gap", Unassessed:"No cycle receipt"}[row.baseline_status] || row.baseline_status || "Not recorded";
  }

  function renderOverviewLists() {
    const ranked = [...data.countries]
      .sort((a,b) => countryCoverageContext(b).pct-countryCoverageContext(a).pct || b.completion_pct-a.completion_pct || a.country.localeCompare(b.country))
      .slice(0,8);
    $("#readiness-list").innerHTML = ranked.map(country => {
      const coverage = countryCoverageContext(country);
      const operational = countryOperationalContext(country);
      const audit = coverage.safeDate ? `strict verified ${coverage.safeLabel}` : "strict audit pending";
      return `<div class="readiness-row">
        <span class="country-code">${esc(country.country)}</span>
        <div class="readiness-progress"><div class="progress-track"><div class="progress-fill" style="width:${Math.min(100, coverage.pct)}%"></div></div><small>${esc(coverage.range)} · ${esc(coverage.countLabel)}</small></div>
        <span class="safe-label"><small>Operational through</small>${esc(operational.throughLabel)}<em>confidence ${esc(operational.countLabel)} · ${esc(audit)}</em></span>
      </div>`;
    }).join("");
    const attention = data.summary.attention_state;
    $("#attention-summary").innerHTML = ["Active","Blocked","Review","Backlog"].map(key => `<div class="attention-tile"><strong>${attention[key] || 0}</strong><span>${key}</span></div>`).join("");
  }

  function sortRows(rows, [key,direction]) {
    return [...rows].sort((a,b) => String(a[key] ?? "").localeCompare(String(b[key] ?? ""), undefined, {numeric:true}) * direction);
  }

  function renderCountries() {
    const term = $("#country-search").value.trim().toLowerCase();
    const [sortKey, direction] = state.countrySort;
    const valueFor = row => sortKey === "cycle_coverage"
      ? countryCoverageContext(row).pct
      : sortKey === "operational_through" ? countryOperationalContext(row).through
      : sortKey === "verified_through" ? countryCoverageContext(row).safeDate
      : sortKey === "tcg_recorded" ? countryTcgContext(row).through : row[sortKey];
    const rows = data.countries
      .filter(r => !term || `${r.country} ${countryName(r.country)}`.toLowerCase().includes(term))
      .sort((a,b) => String(valueFor(a) ?? "").localeCompare(String(valueFor(b) ?? ""), undefined, {numeric:true}) * direction);
    $("#country-table tbody").innerHTML = rows.map(country => {
      const coverage = countryCoverageContext(country);
      return `<tr>
        <td class="country-code">${esc(country.country)}</td>
        <td>${country.sources}</td>
        <td><div class="coverage-cell"><strong>${pill(countryFirstPassContext(country).state)}</strong><small>${esc(countryFirstPassContext(country).label)}</small></div></td>
        <td>${operationalBoundaryCell(country)}</td>
        <td><div class="coverage-cell"><strong>${esc(coverage.countLabel)}</strong><small>${esc(coverage.range)} · ${esc(coverage.operationalState)}</small><div class="progress-track"><div class="progress-fill" style="width:${Math.min(100, coverage.pct)}%"></div></div></div></td>
        <td>${verifiedBoundaryCell(country)}</td>
        <td>${tcgBoundaryCell(country)}</td>
        <td>${currentProcessingCell(country)}</td>
        <td>${pill(country.progress)}</td>
        <td>${esc(country.cycle_wave)}</td>
      </tr>`;
    }).join("");
    $("#country-count").textContent = `${rows.length} country groups`;
  }

  function compactDate(value) {
    if (!value) return "Not recorded";
    const raw = String(value).slice(0, 10);
    const parsed = new Date(`${raw}T00:00:00Z`);
    return Number.isNaN(parsed.getTime()) ? raw : parsed.toLocaleDateString(undefined, {year:"numeric",month:"short",day:"numeric",timeZone:"UTC"});
  }

  function countryName(code) {
    return geometryByCode.get(code)?.name || code;
  }

  function countryProgress(country) {
    if (country.progress) return country.progress;
    if (country.sources && country.baseline_done === country.sources) return "Scraping complete";
    if (country.baseline_done) return "In progress";
    return "Planned";
  }

  function mapStatusClass(country) {
    return `map-status-${slug(countryProgress(country))}`;
  }

  function projectPoint(point) {
    const longitude = Number(point[0]);
    const latitude = Math.max(-90, Math.min(90, Number(point[1])));
    return [((longitude + 180) / 360) * 1000, ((90 - latitude) / 180) * 500];
  }

  function ringPath(ring) {
    return ring.map((point, index) => {
      const [x, y] = projectPoint(point);
      return `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
    }).join("") + "Z";
  }

  function geometryPath(feature) {
    const polygons = feature.type === "Polygon" ? [feature.coordinates] : feature.coordinates;
    return polygons.map(polygon => polygon.map(ringPath).join("")).join("");
  }

  function latestCountryCheck(code) {
    return data.sources
      .filter(row => row.country === code && row.last_checked)
      .map(row => String(row.last_checked).slice(0, 10))
      .sort()
      .at(-1) || "";
  }

  function conciseSourceState(row) {
    if (row.public_state) return row.public_state;
    if (row.downstream_ready === "Yes") return "Ready for research use";
    if (row.attention_state === "Active") return "Update work active";
    if (row.attention_state === "Blocked") return "Access or technical fix needed";
    if (row.attention_state === "Review") return "Focused review flagged";
    if (row.baseline_status === "Done") return row.pipeline_state || "Scraping complete";
    if (row.baseline_status === "Unassessed") return row.pipeline_state && row.pipeline_state !== "Unassessed" ? row.pipeline_state : "Awaiting scheduled run";
    return row.pipeline_state || "Coverage incomplete";
  }

  function shortStageStatus(value) {
    const text = String(value || "").trim();
    if (!text || /not assessed|not verified|unknown/i.test(text)) return "Not checked";
    if (/block/i.test(text)) return "Blocked";
    if (/run|active|in progress/i.test(text)) return "Active";
    if (/partial/i.test(text)) return "Partial";
    if (/wait|pending/i.test(text)) return "Waiting";
    if (/complete|done|passed/i.test(text)) return "Complete";
    return text.length > 28 ? `${text.slice(0, 27)}…` : text;
  }

  function compactSourceRows(rows) {
    const attentionRank = {Blocked:0, Active:1, Review:2, Open:3, Done:4, Ready:5, Backlog:6};
    return [...rows].sort((a, b) => {
      const aRank = attentionRank[a.attention_state || a.public_state] ?? 10;
      const bRank = attentionRank[b.attention_state || b.public_state] ?? 10;
      return aRank - bRank || a.source.localeCompare(b.source);
    }).map(row => `<article class="map-source-row" data-source-index="${data.sources.indexOf(row)}" tabindex="0" role="button" aria-label="Open ${esc(row.source)} source details">
      <div class="map-source-top"><strong>${esc(row.source)}</strong>${pill(sourcePosition(row))}</div>
      <div class="map-source-dates"><span><b>Recorded Notion</b> ${esc(compactDate(row.recorded_notion_boundary))}</span><span><b>Operational through</b> ${esc(compactDate(sourceScrapeRecordDate(row)))}</span><span><b>First-pass verified</b> ${esc(sourceFirstPassDate(row) ? compactDate(sourceFirstPassDate(row)) : "Pending")}</span><span><b>Verified through</b> ${esc(sourceCycleDate(row) ? compactDate(sourceCycleDate(row)) : "Not yet verified")}</span><span><b>Checked</b> ${esc(compactDate(row.last_checked))}</span></div>
      <div class="map-source-pipeline"><span><b>Patchtools</b> ${esc(shortStageStatus(row.patchtools_status))}</span><span><b>Pre-TCG</b> ${esc(shortStageStatus(row.pretcg_status))}</span></div>
      <p><b>Next:</b> ${esc(row.next_action || conciseSourceState(row))}</p>
    </article>`).join("");
  }

  function renderMapCountry(code) {
    const country = countryByCode.get(code);
    if (!country) return;
    state.mapCountry = code;
    $("#map-country-select").value = code;
    $$("#world-map .map-country.tracked").forEach(path => {
      const selected = path.dataset.country === code;
      path.classList.toggle("selected", selected);
      path.setAttribute("aria-pressed", String(selected));
    });
    const rows = data.sources.filter(row => row.country === code);
    const latest = latestCountryCheck(code);
    const attention = Number(country.active || 0) + Number(country.blocked || 0) + Number(country.review || 0);
    const progress = countryProgress(country);
    const coverage = countryCoverageContext(country);
    const operational = countryOperationalContext(country);
    const tcg = countryTcgContext(country);
    const patchtools = countryStageContext(country, "patchtools");
    const pretcg = countryStageContext(country, "pretcg");
    const firstPass = countryFirstPassContext(country);
    const metricItems = [
      [firstPass.label, `first pass · target ${firstPass.target || "not set"}`],
      [operational.throughLabel, `operational through · confidence ${operational.countLabel}`],
      [coverage.safeLabel, "strict verified through · all sources"],
      [coverage.countLabel, `current cycle · ${coverage.range}`],
      [tcg.throughLabel, `TCG recorded · ${tcg.countLabel}`],
      [patchtools.value, "Patchtools"],
      [pretcg.value, "current pre-TCG"],
      [compactDate(latest), "latest review"],
    ];
    $("#map-country-detail").innerHTML = `<div class="map-detail-head">
      <div><span class="country-code">${esc(code)}</span><h3>${esc(countryName(code))}</h3></div>${pill(progress)}
    </div>
    <p class="map-detail-summary">First pass: ${esc(firstPass.label)} · operational record through ${esc(operational.throughLabel)} for ${esc(operational.countLabel)} of sources · ${esc(coverage.summary)} · strict verified through ${esc(coverage.safeLabel)}${attention ? ` · ${attention} need attention` : ""}</p>
    <div class="map-detail-metrics">${metricItems.map(([value, label]) => `<span><b>${esc(value)}</b>${esc(label)}</span>`).join("")}</div>
    <div class="map-source-list" aria-label="Sources tracked for ${esc(countryName(code))}">${compactSourceRows(rows)}</div>`;
    bindSourceOpeners($("#map-country-detail"));
  }

  function renderWorldMap() {
    const root = $("#world-map");
    const select = $("#map-country-select");
    [...countryByCode.keys()].sort((a, b) => countryName(a).localeCompare(countryName(b))).forEach(code => {
      const option = document.createElement("option");
      option.value = code;
      option.textContent = `${countryName(code)} (${code})`;
      select.append(option);
    });
    if (!geometry.length) {
      root.innerHTML = '<p class="empty">Map geometry is unavailable. Use the country picker above.</p>';
    } else {
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 1000 500");
      svg.setAttribute("role", "img");
      svg.setAttribute("aria-labelledby", "map-title map-desc");
      const title = document.createElementNS(svg.namespaceURI, "title");
      title.id = "map-title";
      title.textContent = "World source coverage map";
      const desc = document.createElementNS(svg.namespaceURI, "desc");
      desc.id = "map-desc";
      desc.textContent = "Tracked countries are colored by status. Use Tab and Enter to select one, or use the country picker.";
      svg.append(title, desc);
      geometry.forEach(feature => {
        const path = document.createElementNS(svg.namespaceURI, "path");
        const country = countryByCode.get(feature.id);
        path.setAttribute("d", geometryPath(feature));
        path.setAttribute("class", `map-country${country ? ` tracked ${mapStatusClass(country)}` : ""}`);
        if (country) {
          const coverage = countryCoverageContext(country);
          const operational = countryOperationalContext(country);
          path.dataset.country = feature.id;
          path.setAttribute("tabindex", "0");
          path.setAttribute("role", "button");
          path.setAttribute("aria-pressed", "false");
          path.setAttribute("aria-label", `${feature.name}: ${country.sources} sources, ${countryProgress(country)}, operational through ${operational.throughLabel} for ${operational.countLabel}, strict verified through ${coverage.safeLabel}, ${coverage.countLabel} in ${coverage.range}`);
          const tooltip = document.createElementNS(svg.namespaceURI, "title");
          tooltip.textContent = `${feature.name} · ${country.sources} sources · ${countryProgress(country)}`;
          path.append(tooltip);
          path.addEventListener("click", () => renderMapCountry(feature.id));
          path.addEventListener("keydown", event => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              renderMapCountry(feature.id);
            }
          });
        } else {
          path.setAttribute("aria-hidden", "true");
        }
        svg.append(path);
      });
      root.append(svg);
    }
    select.addEventListener("change", () => select.value && renderMapCountry(select.value));
    const defaultCode = [...countryByCode.keys()].sort((a, b) => latestCountryCheck(b).localeCompare(latestCountryCheck(a)) || a.localeCompare(b))[0];
    if (defaultCode) renderMapCountry(defaultCode);
  }

  function renderSharedSources() {
    const root = $("#shared-source-groups");
    root.innerHTML = ["INT", "REG"].map((code, index) => {
      const rows = data.sources.filter(row => row.country === code);
      const done = rows.filter(row => row.baseline_status === "Done").length;
      const inspected = rows.filter(row => row.first_pass_state && row.first_pass_state !== "Not inspected").length;
      const complete = rows.filter(row => row.first_pass_state === "Complete through target").length;
      return `<details class="shared-source-card"${index === 0 ? " open" : ""}>
        <summary><span><b>${code === "INT" ? "International" : "Regional"}</b><small>${inspected}/${rows.length} inspected · ${complete}/${rows.length} through target · ${done}/${rows.length} prior-cycle covered</small></span><span>${rows.length} sources</span></summary>
        <div class="shared-source-list">${compactSourceRows(rows)}</div>
      </details>`;
    }).join("");
    bindSourceOpeners(root);
  }

  function fillSelect(id, values) {
    const select = $(id); [...new Set(values)].filter(Boolean).sort().forEach(value => select.insertAdjacentHTML("beforeend", `<option value="${esc(value)}">${esc(value)}</option>`));
  }

  function renderSourcePositionBand() {
    const selected = $("#filter-first-pass")?.value || "";
    const counts = data.sources.reduce((result, row) => {
      const key = sourcePosition(row);
      result[key] = (result[key] || 0) + 1;
      return result;
    }, {});
    const tiles = [["", "All sources", data.sources.length], ...sourcePositionOrder.map(position => [position, position, counts[position] || 0])];
    $("#source-position-band").innerHTML = tiles.map(([value, label, count]) => `<button class="position-tile${selected === value ? " active" : ""}" type="button" data-position="${esc(value)}" aria-pressed="${selected === value}"><strong>${count}</strong><span>${esc(label)}</span></button>`).join("");
    $$("#source-position-band .position-tile").forEach(button => button.addEventListener("click", () => {
      $("#filter-first-pass").value = button.dataset.position;
      applySourceFilters();
    }));
  }

  function applySourceFilters() {
    const term = $("#source-search").value.trim().toLowerCase();
    const country = $("#filter-country").value, project = $("#filter-project").value, baseline = $("#filter-baseline").value, attention = $("#filter-attention").value, firstPass = $("#filter-first-pass").value;
    state.filtered = data.sources.filter(r => (!country || r.country === country) && (!project || r.project === project) && (!baseline || r.baseline_status === baseline) && (!attention || r.attention_state === attention) && (!firstPass || r.first_pass_state === firstPass) && (!term || [r.source,r.country,r.problem_summary,r.next_action,r.scraping_progress,r.first_pass_state,r.first_pass_note,r.scrape_record_basis,r.tcg_record_state,r.tcg_record_basis,r.stage].join(" ").toLowerCase().includes(term)));
    state.page = 1; renderSources();
  }
  function renderSources() {
    const [sortKey, direction] = state.sourceSort;
    const sourceValue = row => sortKey === "position" ? sourcePositionRank(row)
      : sortKey === "coverage_through"
      ? sourceCycleDate(row)
      : sortKey === "recorded_through" ? sourceScrapeRecordDate(row)
      : sortKey === "first_pass_verified_through" ? sourceFirstPassDate(row)
      : sortKey === "verified_through" ? sourceCycleDate(row)
      : sortKey === "tcg_recorded" ? sourceTcgRecordDate(row)
      : row[sortKey];
    const rows = [...state.filtered].sort((a,b) => {
      const primary = String(sourceValue(a) ?? "").localeCompare(String(sourceValue(b) ?? ""), undefined, {numeric:true}) * direction;
      return primary || a.country.localeCompare(b.country) || a.source.localeCompare(b.source) || String(a.project || "").localeCompare(String(b.project || ""));
    });
    const pageSize = state.pageSize === Infinity ? Math.max(1, rows.length) : state.pageSize;
    const pages = Math.max(1, Math.ceil(rows.length/pageSize)); state.page = Math.min(state.page,pages);
    const start = (state.page-1)*pageSize, shown = rows.slice(start,start+pageSize);
    $("#source-table tbody").innerHTML = shown.map(r => `<tr data-source-index="${data.sources.indexOf(r)}" tabindex="0" role="button" aria-label="Open ${esc(r.source)} source details">
      <td class="source-primary"><strong>${esc(r.source)}</strong><small>${esc(sourcePlaceLabel(r))} (${esc(r.country)}) · ${esc(r.project)}</small><span class="source-row-hint">Open full details →</span></td>
      <td>${pill(sourcePosition(r))}</td>
      <td><div class="boundary-cell"><strong>${esc(compactDate(r.recorded_notion_boundary))}</strong><small>recorded history</small></div></td>
      <td><div class="boundary-cell"><strong>${esc(compactDate(sourceScrapeRecordDate(r)))}</strong><small>operational record</small></div></td>
      <td><div class="boundary-cell"><strong>${esc(sourceFirstPassDate(r) ? compactDate(sourceFirstPassDate(r)) : "Pending")}</strong><small>target ${esc(compactDate(r.first_pass_target || data.meta.first_pass_target_end))}</small></div></td>
      <td><div class="boundary-cell"><strong>${esc(sourceCycleDate(r) ? compactDate(sourceCycleDate(r)) : "Not yet verified")}</strong><small>continuous receipt floor</small></div></td>
      <td>${esc(displayPipelineStage(r))}</td>
      <td class="action-cell">${pill(r.attention_state)}<p>${esc(r.next_action || "No next action recorded")}</p></td>
      <td>${esc(compactDate(r.last_checked))}</td>
    </tr>`).join("");
    bindSourceOpeners($("#source-table tbody"));
    $("#source-count").textContent = `${rows.length} of ${data.sources.length} sources shown`;
    $("#page-label").textContent = state.pageSize === Infinity ? "All matching sources" : `Page ${state.page} of ${pages}`;
    $("#prev-page").disabled = state.page === 1; $("#next-page").disabled = state.page === pages;
    renderSourcePositionBand();
  }

  function bindSourceOpeners(root) {
    $$('[data-source-index]', root).forEach(element => {
      const open = () => showSource(data.sources[Number(element.dataset.sourceIndex)], element);
      element.addEventListener("click", open);
      element.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          open();
        }
      });
    });
  }

  function closeSourceDetail() {
    const drawer = $("#source-detail");
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    $("#detail-backdrop").classList.remove("open");
    document.body.style.overflow = "";
    if (state.detailTrigger?.isConnected) state.detailTrigger.focus();
    state.detailTrigger = null;
  }

  function showSource(r, trigger = null) {
    const drawer = $("#source-detail");
    state.detailTrigger = trigger || document.activeElement;
    drawer.innerHTML = `<div class="detail-head"><div><h3>${esc(r.source)}</h3><span>${esc(sourcePlaceLabel(r))} (${esc(r.country)}) · ${esc(r.project)} · wave ${esc(r.cycle_wave)}</span><div class="detail-status">${pill(sourcePosition(r))}${pill(r.attention_state)}</div></div><button class="close-detail" aria-label="Close source details">×</button></div>
      <h4 class="detail-section-title">Coverage position</h4>
      <div class="coverage-track">
        <div class="coverage-step"><span>Recorded Notion boundary</span><strong>${esc(compactDate(r.recorded_notion_boundary))}</strong></div>
        <div class="coverage-step"><span>Operational through</span><strong>${esc(compactDate(sourceScrapeRecordDate(r)))}</strong></div>
        <div class="coverage-step"><span>First-pass verified</span><strong>${esc(sourceFirstPassDate(r) ? compactDate(sourceFirstPassDate(r)) : "Pending")}</strong></div>
        <div class="coverage-step"><span>Strict verified through</span><strong>${esc(sourceCycleDate(r) ? compactDate(sourceCycleDate(r)) : "Not yet verified")}</strong></div>
      </div>
      <h4 class="detail-section-title">Processing position</h4>
      <div class="processing-track">
        <div class="processing-step"><span>TCG recorded</span><strong>${esc(sourceTcgRecordDate(r) ? compactDate(sourceTcgRecordDate(r)) : "Not recorded")}</strong></div>
        <div class="processing-step"><span>Patchtools</span><strong>${esc(r.patchtools_status || "Not assessed")}</strong></div>
        <div class="processing-step"><span>Pre-TCG</span><strong>${esc(r.pretcg_status || "Not assessed")}</strong></div>
      </div>
      <div class="detail-grid">${[["First-pass target",r.first_pass_target||data.meta.first_pass_target_end||"Not set"],["Current-month partial",r.current_month_partial_through||"None"],["Current stage",displayPipelineStage(r)],["Method",r.method||"Not recorded"],["Last reviewed",r.last_checked||"Not recorded"],["Language",r.language||"Not recorded"]].map(([k,v])=>`<div class="detail-item"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join("")}</div>
      <div class="detail-copy"><section><h4>First-pass note</h4><p>${esc(r.first_pass_note||"First-pass inspection pending")}</p></section><section><h4>Progress</h4><p>${esc(r.scraping_progress||"No progress note")}</p></section><section><h4>Problem summary</h4><p>${esc(r.problem_summary||"No problem recorded")}</p></section><section><h4>Next action</h4><p>${esc(r.next_action||"No action recorded")}</p></section></div>`;
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    $("#detail-backdrop").classList.add("open");
    document.body.style.overflow = "hidden";
    $(".close-detail",drawer).addEventListener("click", closeSourceDetail);
    $(".close-detail",drawer).focus();
  }

  function renderAttention() {
    const active = data.sources.filter(r=>r.attention_state === "Active");
    const blockers = data.sources.filter(r=>["Blocked","Review"].includes(r.attention_state));
    const workHtml = rows => rows.length ? rows.map(r=>`<div class="work-item"><div class="work-top"><span><span class="work-source">${esc(r.source)}</span> <span class="work-country">${esc(r.country)}</span></span>${pill(r.attention_state)}</div><span class="work-label">Problem</span><p>${esc(r.problem_summary||r.scraping_progress||"No detail recorded")}</p><span class="work-label">Next</span><p>${esc(r.next_action||"Review the latest evidence")}</p></div>`).join("") : '<p class="empty">Nothing in this queue.</p>';
    $("#active-list").innerHTML=workHtml(active); $("#blocker-list").innerHTML=workHtml(blockers);
    $("#active-count").textContent=active.length; $("#blocker-count").textContent=blockers.length;
  }
  function renderCycle() { $("#cycle-list").innerHTML=data.waves.map(w=>`<article class="wave-card"><span class="wave-number">Wave ${w.wave}</span><strong>${esc(w.countries)}</strong><p>${esc(w.rule)}</p></article>`).join(""); }

  function csvEscape(value) { const text=String(value??""); return /[",\n]/.test(text) ? `"${text.replace(/"/g,'""')}"` : text; }
  function exportCsv() {
    const keys=["country","source","project","recorded_notion_boundary","first_pass_target","first_pass_verified_through","current_month_partial_through","first_pass_state","first_pass_note","baseline_status","scrape_record_through","scrape_record_state","scrape_record_basis","boundary_check","scrape_verified_through","tcg_record_through","tcg_record_state","tcg_record_basis","stage","last_checked","method","pipeline_state","attention_state","needs_revisit","patchtools_status","pretcg_status","downstream_ready","problem_summary","next_action"];
    const csv=[keys.join(","),...state.filtered.map(r=>keys.map(k=>csvEscape(r[k])).join(","))].join("\n");
    const url=URL.createObjectURL(new Blob([csv],{type:"text/csv;charset=utf-8"})); const a=document.createElement("a"); a.href=url;a.download="source_tracker_filtered.csv";a.click();URL.revokeObjectURL(url);
  }

  function bind() {
    fillSelect("#filter-country",data.sources.map(r=>r.country)); fillSelect("#filter-project",data.sources.map(r=>r.project)); fillSelect("#filter-baseline",data.sources.map(r=>r.baseline_status)); fillSelect("#filter-attention",data.sources.map(r=>r.attention_state)); fillSelect("#filter-first-pass",data.sources.map(r=>r.first_pass_state));
    ["#source-search","#filter-country","#filter-project","#filter-baseline","#filter-attention","#filter-first-pass"].forEach(id=>$(id).addEventListener(id.includes("search")?"input":"change",applySourceFilters));
    $("#country-search").addEventListener("input",renderCountries);
    $("#clear-filters").addEventListener("click",()=>{["#source-search","#filter-country","#filter-project","#filter-baseline","#filter-attention","#filter-first-pass"].forEach(id=>$(id).value="");applySourceFilters();});
    $("#prev-page").addEventListener("click",()=>{state.page--;renderSources();}); $("#next-page").addEventListener("click",()=>{state.page++;renderSources();});
    $("#source-page-size").addEventListener("change", event => {
      state.pageSize = event.target.value === "all" ? Infinity : Number(event.target.value);
      state.page = 1;
      renderSources();
    });
    $("#show-all-sources").addEventListener("click", () => {
      ["#source-search","#filter-country","#filter-project","#filter-baseline","#filter-attention","#filter-first-pass"].forEach(id => $(id).value = "");
      applySourceFilters();
      $("#source-search").focus();
    });
    $("#detail-backdrop").addEventListener("click", closeSourceDetail);
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && $("#source-detail").classList.contains("open")) closeSourceDetail();
    });
    $("#export-csv").addEventListener("click",exportCsv);
    $$("#source-table th").forEach(th=>th.addEventListener("click",()=>{const k=th.dataset.sort;state.sourceSort=[k,state.sourceSort[0]===k?-state.sourceSort[1]:1];renderSources();}));
    $$("#country-table th").forEach(th=>th.addEventListener("click",()=>{const k=th.dataset.sort;state.countrySort=[k,state.countrySort[0]===k?-state.countrySort[1]:1];renderCountries();}));
  }

  prepareSourceDirectory();renderHeader();renderDonut();renderPipeline();renderOverviewLists();renderWorldMap();renderSharedSources();renderCountries();renderSources();renderAttention();renderCycle();bind();
})();
