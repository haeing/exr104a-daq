"use strict";
const $ = id => document.getElementById(id);
const activeStates = new Set(["connecting", "running", "stopping"]);
const statusLabels = {idle:"Ready", connecting:"Connecting to scope", running:"Acquiring · Waiting for trigger", stopping:"Stopping", complete:"Acquisition complete", stopped:"Acquisition stopped", failed:"Acquisition failed"};
const slopes = {rising:"Rising", falling:"Falling", either:"Either"};
let busy = false;
let sending = false;
let lastState = null;
let lastWaveKey = "";
let lastHistKey = "";
const format = (n, digits=2) => Number(n).toLocaleString("en-US", {maximumFractionDigits:digits});

function showError(message) { $("error").hidden = !message; $("error").textContent = message || ""; }
function buttons() {
  $("start").disabled = busy || sending;
  $("stop").disabled = !busy || sending || lastState?.status === "stopping";
  $("settings").disabled = busy || sending;
}
async function post(path, data) {
  const response = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
}
$("run-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (sending || busy) return;
  const data = Object.fromEntries(new FormData(event.target));
  sending = true; buttons(); showError("");
  try { await post("/api/start", data); busy = true; }
  catch (error) { showError(error.message); }
  finally { sending = false; buttons(); }
});
$("stop").addEventListener("click", async () => {
  sending = true; buttons();
  try { await post("/api/stop", {}); }
  catch (error) { showError(error.message); }
  finally { sending = false; buttons(); }
});

function svgElement(name, attributes={}, text="") {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key,value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  if (text !== "") node.textContent = text;
  return node;
}
function axisChart(container, xMin, xMax, yMin, yMax, height, xLabel, yLabel) {
  const width=880, left=65, right=24, top=20, bottom=45;
  if (xMax===xMin) { xMin-=0.5; xMax+=0.5; }
  if (yMax===yMin) { yMin-=0.5; yMax+=0.5; }
  const svg=svgElement("svg", {viewBox:`0 0 ${width} ${height}`, role:"img", "aria-label":`${xLabel}, ${yLabel}`});
  const x = value => left+(value-xMin)/(xMax-xMin)*(width-left-right);
  const y = value => height-bottom-(value-yMin)/(yMax-yMin)*(height-top-bottom);
  for(let n=0;n<=4;n++) {
    const value=yMin+(yMax-yMin)*n/4, py=y(value);
    svg.append(svgElement("line",{x1:left,x2:width-right,y1:py,y2:py,stroke:"#e8eeea","stroke-dasharray":"3 4"}));
    svg.append(svgElement("text",{x:left-11,y:py+4,"text-anchor":"end",fill:"#82978d","font-size":11}, format(value,2)));
  }
  for(let n=0;n<=5;n++) {
    const value=xMin+(xMax-xMin)*n/5;
    svg.append(svgElement("text",{x:x(value),y:height-bottom+20,"text-anchor":"middle",fill:"#82978d","font-size":11},format(value,3)));
  }
  svg.append(svgElement("text",{x:width/2,y:height-5,"text-anchor":"middle",fill:"#789084","font-size":10},xLabel));
  svg.append(svgElement("text",{x:left,y:12,fill:"#789084","font-size":10},yLabel));
  container.replaceChildren(svg);
  return {svg,x,y,height,width,left,right,top,bottom};
}
function drawHistogram(histogram, total) {
  if(!histogram.edges.length) {
    if (lastHistKey !== "empty") $("histogram").innerHTML='<div class="empty"><span class="empty-bars" aria-hidden="true">▂ ▅ █ ▆ ▃</span><strong>Waiting for the first waveform</strong><p>Choose a channel and trigger, then start acquisition.</p></div>';
    lastHistKey="empty"; return;
  }
  const key=JSON.stringify([histogram.metric,total,histogram.edges.length]);
  if(key===lastHistKey) return;
  lastHistKey=key;
  const edges=histogram.edges.map(n=>n*1000), counts=histogram.counts;
  const c=axisChart($("histogram"),edges[0],edges[edges.length-1],0,Math.max(1,...counts)*1.12,300,"Pulse height / Voltage (mV)","EVENTS");
  counts.forEach((count,i)=>{
    const bar=svgElement("rect",{x:c.x(edges[i])+0.5,y:c.y(count),width:Math.max(0.5,c.x(edges[i+1])-c.x(edges[i])-1),height:c.y(0)-c.y(count),fill:"#32967e",rx:1});
    bar.append(svgElement("title",{},`${format(edges[i],3)} – ${format(edges[i+1],3)} mV: ${count} events`)); c.svg.append(bar);
  });
}
function drawWaveform(state) {
  const wave=state.waveform;
  if(!wave) {
    if(lastWaveKey!=="empty") $("waveform").innerHTML='<div class="empty"><span class="empty-wave" aria-hidden="true">─∿─</span><strong>No waveform captured yet</strong><p>Preview and pulse heights use the unbinned waveform.</p></div>';
    lastWaveKey="empty"; return;
  }
  const key=`${state.started_utc}:${state.saved}`;
  if(key===lastWaveKey) return;
  lastWaveKey=key;
  const volts=wave.voltage_v.map(n=>n*1000), xs=wave.time_us;
  let min=Math.min(...volts), max=Math.max(...volts);
  const padding=Math.max((max-min)*0.12,0.01);
  const c=axisChart($("waveform"),xs[0],xs[xs.length-1],min-padding,max+padding,220,"TIME (μs)","VOLTAGE (mV)");
  const baseline=state.latest.baseline_v*1000;
  c.svg.append(svgElement("line",{x1:c.left,x2:c.width-c.right,y1:c.y(baseline),y2:c.y(baseline),stroke:"#b0bdb6","stroke-dasharray":"5 4"}));
  c.svg.append(svgElement("polyline",{points:xs.map((x,i)=>`${c.x(x)},${c.y(volts[i])}`).join(" "),fill:"none",stroke:"#267f82","stroke-width":1.4,"stroke-linejoin":"round"}));
}
function render(state) {
  // A new run may contain the same event count as the previous run.
  if(state.started_utc!==lastState?.started_utc) {lastHistKey="";lastWaveKey="";}
  lastState=state; busy=activeStates.has(state.status); buttons();
  $("status").textContent=statusLabels[state.status] || state.status;
  $("status-dot").className="status-dot"+(busy?" active":state.status==="failed"?" failed":"");
  $("mode").textContent=state.demo?"DEMO · SIMULATED DATA":"HARDWARE MODE";
  $("mode").className="mode"+(state.demo?" demo":"");
  $("scope-ip").textContent=state.ip;
  $("saved").textContent=format(state.saved,0); $("target").textContent=`/ ${format(state.target,0)}`;
  $("rate").textContent=state.saved?format(state.rate_hz,2):"—";
  const seconds=Math.floor(state.elapsed_s);
  $("elapsed").textContent=`${String(Math.floor(seconds/60)).padStart(2,"0")}:${String(seconds%60).padStart(2,"0")}`;
  $("points").textContent=state.points?`${format(state.stored_points,0)} saved / ${format(state.points,0)} acquired points`:"Waiting for waveform";
  $("bin-info").textContent=state.actual_bin_width_us?`Fixed run bin width: ${format(state.actual_bin_width_us,8)} μs.`:"Gate and bin edges stay fixed throughout each run. 0 locks the first event's native interval.";
  $("next-file").value=state.next_filename;
  $("data-dir").textContent=state.data_dir;
  const gb=state.bytes>=1e9; $("file-size").textContent=format(state.bytes/(gb?1e9:1e6),2); $("file-unit").textContent=gb?"GB":"MB";
  const progress=state.target?100*state.saved/state.target:0;
  $("progress").style.width=`${progress}%`;
  $("progress").parentElement.setAttribute("aria-valuenow",String(Math.round(progress)));
  $("output").textContent=state.output || "The output path will appear when acquisition starts";
  $("run-started").textContent=state.started_utc?new Date(state.started_utc).toLocaleString("en-GB",{year:"numeric",month:"short",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false,timeZoneName:"short"}):"—";
  $("run-started").dateTime=state.started_utc || "";
  $("hist-count").textContent=`${format(state.saved,0)} events`;
  $("mean").textContent=state.histogram.mean===null?"—":`${format(state.histogram.mean*1000,3)} mV`;
  $("std").textContent=state.histogram.std===null?"—":`${format(state.histogram.std*1000,3)} mV`;
  if(state.config) $("applied").textContent=`CH ${state.config.channel} · ${format(state.config.trigger_level*1000,4)} mV · ${slopes[state.config.slope]} · −${format(state.config.gate_before_us,3)} / +${format(state.config.gate_after_us,3)} μs`;
  $("baseline-info").textContent=state.baseline_samples?`Baseline: mean of the first ${format(state.baseline_samples,0)} samples`:"Baseline: mean of the first 10%";
  if(state.error) showError(state.error);
  else if(busy || state.status==="complete") showError("");
  drawHistogram(state.histogram,state.saved); drawWaveform(state);
}
async function poll() {
  try {
    const bins=Number($("bins").value);
    if(!Number.isInteger(bins) || bins<5 || bins>500) throw new Error("Bins must be an integer between 5 and 500.");
    const query=new URLSearchParams({metric:$("metric").value,bins:String(bins)});
    const response=await fetch(`/api/state?${query}`);
    const state=await response.json();
    if(!response.ok) throw new Error(state.error || "Unable to read acquisition status");
    render(state);
    $("updated").textContent=`Updated ${new Date().toLocaleTimeString("en-GB",{hour12:false})}`;
  } catch(error) {
    $("status").textContent="Status unavailable";
    $("status-dot").className="status-dot failed";
    $("start").disabled=true;
    showError(error.message);
  } finally {setTimeout(poll,1000);}
}
poll();
