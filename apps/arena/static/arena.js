"use strict";
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const phaseNames = {preparing:"準備中",ready:"準備完了",running:"実行中",grading:"採点中",feedback:"結果を返却",passed:"全件合格",error:"エラー",stopped:"停止",limit:"上限に到達"};
const channelNames = {text:"ASSISTANT",reasoning:"THINKING",code:"PTC / CODE",tool:"TOOL CALL",result:"TOOL RESULT",event:"LIFECYCLE",diagnostic:"DIAGNOSTIC",grade:"EXTERNAL GRADER",error:"ERROR"};
let current = null, csrf = "", lastSequence = 0, displayedRun = null;
const follow = {official:true,engy:true};
const lastEntries = {};
const number = value => new Intl.NumberFormat("en-US").format(value || 0);
const elapsed = seconds => seconds == null ? "00:00" : `${String(Math.floor(seconds / 60)).padStart(2,"0")}:${String(Math.floor(seconds % 60)).padStart(2,"0")}`;
function notice(text, info = false) {const target=$("#notice");target.textContent=text;target.hidden=!text;target.classList.toggle("info",info);}
async function api(path, data) {
  const response = await fetch(`/api/${path}`, data === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json","X-Arena-Token":csrf},body:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
function append(lane, channel, text) {
  if (!lane || !text) return;
  const pane=$(`.lane[data-lane="${lane}"]`), transcript=$(".transcript",pane);
  if (!transcript) return;
  $(".empty",transcript)?.remove();
  let entry=lastEntries[lane];
  const merge=["text","reasoning","code"].includes(channel);
  if (!entry || entry.channel!==channel || !merge) {
    const node=document.createElement("div");node.className=`entry ${channel}`;
    const label=document.createElement("div");label.className="entry-label";label.textContent=channelNames[channel] || channel.toUpperCase();
    const content=document.createElement("pre");node.append(label,content);transcript.append(node);
    entry={channel,node,content};lastEntries[lane]=entry;
  }
  entry.content.append(document.createTextNode(text));
  if (entry.content.textContent.length > 180000) entry.content.textContent=entry.content.textContent.slice(-160000);
  while (transcript.childElementCount>300) transcript.firstElementChild.remove();
  if (follow[lane]) transcript.scrollTop=transcript.scrollHeight;
}
function event(value) {
  if (value.seq && value.seq<=lastSequence) return;
  if (value.seq) lastSequence=value.seq;
  if (value.kind==="output") append(value.lane,value.channel,value.text);
  if (value.kind==="error") {
    if (value.lane) append(value.lane,"error",value.message);
    else notice(value.message);
  }
  if (value.kind==="phase") append(value.lane,"event",`${phaseNames[value.phase] || value.phase}${value.message ? ` · ${value.message}` : ""}\n`);
  if (value.kind==="grade") append(value.lane,"grade",`${value.report.passed}/${value.report.total} passed · ${(value.report.durationMs/1000).toFixed(2)}s\n${value.report.cases.filter(c=>c.status!=="passed").map(c=>`${c.id}: ${c.error}`).join("\n")}`);
  if (value.kind==="feedback") append(value.lane,"event",`採点JSONを同じセッションへ返却 · SHA256 ${value.reportSha256.slice(0,12)}\n`);
  if (value.kind==="finished") refresh();
}
function render(state, replay = false) {
  current=state;
  if (state.csrf) csrf=state.csrf;
  $("#demo-banner").hidden=!state.demo;
  if (state.runId && displayedRun!==state.runId) {
    displayedRun=state.runId;lastSequence=0;
    for (const pane of $$(".lane")) {
      const frame=$(".dsh-frame",pane);frame.hidden=true;frame.removeAttribute("src");
      $(".native-placeholder",pane).hidden=false;
    }
    delete lastEntries.official;delete lastEntries.engy;
    replay=true;
  }
  $("#start").disabled=state.active || !state.verification.ready || !Object.values(state.providers).every(p=>p.hasKey);
  $("#stop").disabled=!state.active&&!Object.values(state.lanes).some(lane=>lane.uiReady);
  $("#settings").disabled=state.active;
  $("#resources").textContent=`各 ${state.limits.cpus} vCPU / ${state.limits.memory_mb} MiB`;
  $("#verifier-resources").textContent=`採点 ${state.limits.verifier_cpus} vCPU / ${state.limits.verifier_memory_mb} MiB`;
  const verified=$("#verification");verified.textContent=state.verification.ready ? `✓ Lean ${state.verification.theorems}定理 / 実装テスト済` : "検証が必要";verified.classList.toggle("ok",state.verification.ready);verified.title=state.verification.reason || state.verification.scope || "";
  $("#payload-match").textContent=state.firstRequestsEqual ? "✓ 初回リクエスト完全一致" : "初回リクエスト一致を待機";
  $("#payload-match").classList.toggle("ok",state.firstRequestsEqual);
  $("#skew").textContent=`送信差 ${state.dispatchSkewMs==null ? "—" : `${state.dispatchSkewMs.toFixed(2)} ms`}`;
  $("#run-id").textContent=state.runId || "NO RUN / LOCAL WORKSPACE";
  if (state.task) $("#task").textContent=state.task;
  if (state.error) notice(state.error);
  else if (!state.verification.ready) notice(state.verification.reason || "先に verify.sh を実行してください。");
  else if (!Object.values(state.providers).every(p=>p.hasKey)) notice("接続設定でEngy / DeepSeekのAPIキーを設定してください。",true);
  else notice("");
  $("#run-links").replaceChildren();
  if (state.runId) for (const [file,label] of [["events.jsonl","実行ログ ↗"],["manifest.json","固定条件 ↗"],...(!state.active ? [["summary.json","結果JSON ↗"]] : [])]) {
    const link=document.createElement("a");link.href=`/artifacts/${state.runId}/${file}`;link.textContent=label;$("#run-links").append(link);
  }
  for (const id of ["official","engy"]) {
    const pane=$(`.lane[data-lane="${id}"]`), lane=state.lanes[id], provider=state.providers[id];
    $(".model-id",pane).textContent=provider.model;
    $(".rate-detail",pane).textContent=`$/M: ${provider.rates.input} / ${provider.rates.output} / ${provider.rates.cached}`;
    if (!lane) continue;
    const frame=$(".dsh-frame",pane), open=$(".open-native",pane);
    if(lane.uiReady){
      const target=lane.uiUrl;
      if(frame.getAttribute("src")!==target)frame.src=target;
      frame.hidden=false;$(".native-placeholder",pane).hidden=true;
      open.hidden=false;open.href=target;
    }else if(frame.getAttribute("src")){
      frame.hidden=true;frame.removeAttribute("src");open.hidden=true;
      $(".native-placeholder",pane).hidden=false;
      $(".native-placeholder",pane).textContent=lane.message || "Dshの表示を停止しました。実行ログと結果JSONは保存されています。";
    }
    pane.dataset.phase=lane.phase;
    $(".phase",pane).textContent=phaseNames[lane.phase] || lane.phase;
    $(".elapsed",pane).textContent=elapsed(lane.elapsedSeconds);
    $(".round",pane).textContent=`ROUND ${lane.round}`;
    $(".ttft",pane).textContent=`TTFT ${lane.metrics.ttftMs==null ? "—" : `${(lane.metrics.ttftMs/1000).toFixed(2)}s`}`;
    for (const node of $$("[data-metric]",pane)) {
      const key=node.dataset.metric,value=lane.metrics[key];
      node.textContent=key==="cost" ? `$${Number(value).toFixed(4)}` : key==="cachePercent" ? `${value.toFixed(1)}%` : ["tokensPerSecond","pingMs"].includes(key) ? (value==null ? "—" : value.toFixed(1)) : number(value);
    }
    $("[data-metric='pingMs']",pane).title=lane.metrics.pingError || lane.metrics.pingAt || "";
    $("[data-metric='cost']",pane).title=lane.metrics.incompleteRequests ? `${lane.metrics.incompleteRequests}件は進行中またはusage未完了。料金は判明分の概算です。` : "設定レートによる概算";
    if (lane.grade) {
      const report=lane.grade, strip=$(".grade-strip",pane);
      strip.classList.toggle("passed",report.allPassed);strip.classList.toggle("failed",!report.allPassed);
      $(".grade-icon",pane).textContent=report.allPassed ? "✓" : "↻";
      $(".grade-title",pane).textContent=`${report.passed} / ${report.total} passed${lane.phase==="passed" ? " · 完了" : ""}`;
      $(".grade-detail",pane).textContent=`採点 ${(report.durationMs/1000).toFixed(2)}s${report.cpuSeconds!=null ? ` · CPU ${report.cpuSeconds.toFixed(2)}s` : ""}${report.peakMemoryBytes ? ` · ${(report.peakMemoryBytes/1048576).toFixed(0)} MiB` : ""} · ${lane.gradeCount}回目`;
      $(".show-grade",pane).disabled=false;
    }
  }
  if (replay) for (const value of state.events) event(value);
}
async function refresh(){try{render(await api("state"),true);}catch(error){notice(error.message);}}
function connect() {
  const socket=new WebSocket(`ws://${location.host}/ws`);
  socket.onopen=()=>{$("#connection").textContent="● LIVE";$("#connection").classList.add("live");};
  socket.onmessage=message=>{const value=JSON.parse(message.data);if(value.kind==="snapshot"||value.kind==="status")render(value.state,value.kind==="snapshot");else if(value.kind==="reconnect")socket.close();else event(value);};
  socket.onclose=()=>{$("#connection").textContent="再接続中";$("#connection").classList.remove("live");setTimeout(connect,1200);};
}
$("#start").addEventListener("click",async()=>{$("#start").disabled=true;try{await api("start",{});await refresh();}catch(error){notice(error.message);$("#start").disabled=false;}});
$("#stop").addEventListener("click",async()=>{$("#stop").disabled=true;try{await api("stop",{});await refresh();}catch(error){notice(error.message);}});
$("#settings").addEventListener("click",()=>{
  for(const fieldset of $$("[data-provider]")){const provider=current.providers[fieldset.dataset.provider];for(const name of ["baseUrl","model"])$(`[name='${name}']`,fieldset).value=provider[name];for(const name of ["input","output","cached"])$(`[name='${name}']`,fieldset).value=provider.rates[name];$("[name='key']",fieldset).placeholder=provider.hasKey ? "設定済み（空欄なら維持）" : "API keyを入力";}
  $("#settings-error").textContent="";$("#settings-dialog").showModal();
});
$("#settings-form").addEventListener("submit",async e=>{
  e.preventDefault();const providers={};
  for(const fieldset of $$("[data-provider]")){const item={rates:{}};for(const name of ["baseUrl","model","key"])item[name]=$(`[name='${name}']`,fieldset).value;for(const name of ["input","output","cached"])item.rates[name]=$(`[name='${name}']`,fieldset).value;providers[fieldset.dataset.provider]=item;}
  try{await api("config",{providers,saveKeys:$("#save-keys").checked});for(const input of $$("input[name='key']"))input.value="";$("#settings-dialog").close();await refresh();}catch(error){$("#settings-error").textContent=error.message;}
});
for(const button of $$(".close-dialog"))button.addEventListener("click",()=>button.closest("dialog").close());
for(const pane of $$(".lane")){
  const id=pane.dataset.lane;
  $(".show-grade",pane).addEventListener("click",()=>{
    const report=current.lanes[id].grade;$("#grade-dialog-title").textContent=`${current.providers[id].name} — ${report.passed}/${report.total}`;
    $("#grade-summary").textContent=`${(report.durationMs/1000).toFixed(2)}s · snapshot ${report.snapshotSha256.slice(0,16)}`;$("#grade-cases").replaceChildren();
    for(const test of report.cases){const row=document.createElement("div");row.className="case-row";const symbol=document.createElement("span");symbol.className=test.status==="passed"?"pass":"fail";symbol.textContent=test.status==="passed"?"✓":"×";const name=document.createElement("span");name.className="case-name";name.textContent=test.id;const duration=document.createElement("span");duration.className="case-time";duration.textContent=`${test.durationMs.toFixed(0)}ms`;row.append(symbol,name,duration);$("#grade-cases").append(row);if(test.error){const error=document.createElement("div");error.className="case-error";error.textContent=test.error;$("#grade-cases").append(error);}}
    $("#grade-json").textContent=JSON.stringify(report,null,2);$("#grade-dialog").showModal();
  });
}
refresh().then(connect);
