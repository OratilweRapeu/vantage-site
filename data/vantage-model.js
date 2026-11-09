/* vantage-model.js
   Shared scoring for the Model page (performance.html) and the Football > Model tab.

   WHERE THE NUMBERS COME FROM, in order:
   1. data/prediction_log.json   the permanent record, written by update_model_log.py. Predictions are
                                  locked once a match kicks off, so this is the honest long-run history.
   2. predictions.json           the file the home page already uses. Every fixture that has both a
                                  prediction and a final score in that file is scored on the spot.
   3. SNAPSHOT (below)           the matchweek 6 predictions and results, so the page is never empty.
   Nothing here is invented: no file, no numbers. */
(function(w){
'use strict';
var SNAPSHOT=/*SNAP*/[{"c":"Premier League","md":6,"k":"2026-10-10T11:30:00Z","h":"Arsenal","hc":"ARS","a":"Leeds","ac":"LEE","p":[39,29,32],"s":[2,1]},{"c":"Premier League","md":6,"k":"2026-10-10T14:00:00Z","h":"Aston Villa","hc":"AVL","a":"Brentford","ac":"BRE","p":[21,24,55],"s":[2,2]},{"c":"Premier League","md":6,"k":"2026-10-10T14:00:00Z","h":"Chelsea","hc":"CHE","a":"Bournemouth","ac":"BOU","p":[46,22,32],"s":[5,1]},{"c":"Premier League","md":6,"k":"2026-10-10T14:00:00Z","h":"Ipswich","hc":"IPS","a":"Fulham","ac":"FUL","p":[42,24,34],"s":[2,1]},{"c":"Premier League","md":6,"k":"2026-10-10T14:00:00Z","h":"Sunderland","hc":"SUN","a":"Brighton","ac":"BHA","p":[18,18,64],"s":[0,2]},{"c":"Premier League","md":6,"k":"2026-10-10T16:30:00Z","h":"Man United","hc":"MUN","a":"Tottenham","ac":"TOT","p":[56,24,20],"s":[1,1]},{"c":"Premier League","md":6,"k":"2026-10-11T13:00:00Z","h":"Crystal Palace","hc":"CRY","a":"Forest","ac":"NFO","p":[35,27,38],"s":[3,1]},{"c":"Premier League","md":6,"k":"2026-10-11T13:00:00Z","h":"Hull","hc":"HUL","a":"Everton","ac":"EVE","p":[36,31,33],"s":[2,2]},{"c":"Premier League","md":6,"k":"2026-10-11T15:30:00Z","h":"Liverpool","hc":"LIV","a":"Man City","ac":"MCI","p":[34,26,40],"s":[0,3]},{"c":"Premier League","md":6,"k":"2026-10-12T19:00:00Z","h":"Coventry","hc":"COV","a":"Newcastle","ac":"NEW","p":[22,24,54],"s":null}]/*END*/;
var SNAP_UPDATED='2026-10-12T07:00:00+00:00';

function get(u){return fetch(u,{cache:'no-cache'}).then(function(r){return r.ok?r.json():Promise.reject();});}
function outcome(s){return s[0]>s[1]?0:s[0]<s[1]?2:1;}
function pick(p){var b=0;for(var i=1;i<3;i++)if(p[i]>p[b])b=i;return b;}
function round(v,d){var f=Math.pow(10,d);return Math.round(v*f)/f;}

/* ---- read the home-page predictions.json (single league or multi-competition) ---- */
function fromPreds(d){
  var comps=d&&d.competitions?Object.keys(d.competitions).map(function(k){return d.competitions[k];}):[d],out=[];
  comps.forEach(function(c){
    var fx=c.fixtures||[],rs=(c.results&&c.results.fixtures)||[],md=(c.results&&c.results.matchweek)||c.matchweek||null,res={};
    rs.forEach(function(r){res[r.home.code+'|'+r.away.code+'|'+r.kickoff]=r;});
    fx.forEach(function(f){
      if(!f.pct)return;
      var r=res[f.home.code+'|'+f.away.code+'|'+f.kickoff];
      out.push({c:c.name||'Premier League',md:md,k:f.kickoff,h:f.home.name,hc:f.home.code,a:f.away.name,ac:f.away.code,p:f.pct,
        s:(r&&r.status==='FT'&&r.score)?r.score:null});
    });
  });
  return out;
}

/* ---- all the maths ---- */
function summarise(list){
  var done=list.filter(function(m){return m.s;}).sort(function(a,b){return a.k<b.k?-1:a.k>b.k?1:0;});
  var n=done.length,hit=0,home=0,rps=0,ll=0,calPts=[],weeks={},wkOrder=[],comps={},bands=[[0,40,'under 40%'],[40,50,'40-50%'],[50,60,'50-60%'],[60,101,'60%+']].map(function(b){return {lo:b[0],hi:b[1],l:b[2],n:0,hit:0};});
  var rows=done.map(function(m){
    var sum=m.p[0]+m.p[1]+m.p[2],pn=m.p.map(function(v){return v/sum;}),o=outcome(m.s),pk=pick(m.p),ok=pk===o;
    var c1=pn[0],c2=pn[0]+pn[1],o1=o===0?1:0,o2=o<=1?1:0;
    rps+=((c1-o1)*(c1-o1)+(c2-o2)*(c2-o2))/2;
    ll+=-Math.log(Math.max(pn[o],0.01));
    if(ok)hit++; if(o===0)home++;
    for(var i=0;i<3;i++)calPts.push([pn[i]*100,i===o?1:0]);
    var top=Math.max.apply(null,m.p);
    bands.forEach(function(b){if(top>=b.lo&&top<b.hi){b.n++;if(ok)b.hit++;}});
    var wk=m.c+'|'+(m.md||m.k.slice(0,10));
    if(!weeks[wk]){weeks[wk]={l:m.md?'MW'+m.md:m.k.slice(5,10),n:0,hit:0,home:0};wkOrder.push(wk);}
    weeks[wk].n++;if(ok)weeks[wk].hit++;if(o===0)weeks[wk].home++;
    var cm=comps[m.c]||(comps[m.c]={l:m.c,n:0,hit:0});cm.n++;if(ok)cm.hit++;
    return {m:m,pk:pk,o:o,ok:ok};
  });
  var cum=0,ch=0,cb=0,wl=wkOrder.map(function(k){var x=weeks[k];cum+=x.n;ch+=x.hit;cb+=x.home;return {l:x.l,acc:round(ch/cum*100,1),base:round(cb/cum*100,1),n:cum};});
  var bins=[];for(var b=0;b<10;b++){var pts=calPts.filter(function(q){return q[0]>=b*10&&q[0]<(b+1)*10+(b===9?1:0);});
    if(pts.length>=8)bins.push({x:round(pts.reduce(function(s,q){return s+q[0];},0)/pts.length,1),y:round(pts.reduce(function(s,q){return s+q[1];},0)/pts.length*100,1),n:pts.length});}
  return {n:n,hit:hit,acc:n?hit/n*100:0,base:n?home/n*100:0,rps:n?rps/n:0,ll:n?ll/n:0,rows:rows,bins:bins,
    bands:bands.filter(function(b){return b.n>0;}).map(function(b){return {l:b.l,n:b.n,acc:round(b.hit/b.n*100,1)};}),
    weeks:wl,comps:Object.keys(comps).map(function(k){var c=comps[k];return {l:c.l,n:c.n,acc:round(c.hit/c.n*100,1)};}),
    pending:list.length-n,late:list.filter(function(m){return m.late;}).length};
}

function pack(mode,list,updated,bootstrap){return {mode:mode,updated:updated||'',bootstrap:!!bootstrap,list:list,S:summarise(list)};}

function load(){
  return get('data/prediction_log.json').then(function(d){
      var l=Array.isArray(d)?d:d.matches;if(!l||!l.length)throw 0;return pack('log',l,d.updated||'',false);})
    .catch(function(){return get('predictions.json').then(function(d){
      var l=fromPreds(d);if(!l.length)throw 0;return pack('file',l,d.updated||'',d.bootstrap);});})
    .catch(function(){return pack('snapshot',SNAPSHOT,SNAP_UPDATED,true);});
}

/* ---- shared display bits ---- */
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function kpis(S){
  var d=S.acc-S.base;
  return [
    {v:S.n?S.acc.toFixed(1)+'%':'n/a',l:'Outcome accuracy (home / draw / away)',n:S.hit+' of '+S.n+' calls right'},
    {v:S.n?(d>=0?'+':'')+d.toFixed(1)+' pts':'n/a',l:'Against always picking the home win',n:'home-win baseline '+S.base.toFixed(1)+'%'},
    {v:S.n?S.rps.toFixed(3):'n/a',l:'Ranked probability score',n:'lower is better'},
    {v:S.n?S.ll.toFixed(2):'n/a',l:'Log loss',n:'lower is better, 1.10 is a coin-flip guess'},
    {v:String(S.n),l:'Matches scored',n:S.pending?S.pending+' still to be played':'every prediction on file'}];
}
var LAB=['H','D','A'];
function tableHTML(S,limit){
  var r=S.rows.slice().reverse();if(limit)r=r.slice(0,limit);
  if(!r.length)return '<div class="vm-empty">No finished matches have been scored yet. They appear here as results come in.</div>';
  return '<div class="vm-t">'+r.map(function(x){var m=x.m,p=m.p;
    var d=new Date(m.k),ds=isNaN(d)?'':d.toLocaleDateString('en-GB',{day:'numeric',month:'short'});
    return '<div class="vm-r"><span class="vm-d">'+ds+'</span>'+
      '<span class="vm-f"><b>'+esc(m.h)+'</b><i>v</i><b>'+esc(m.a)+'</b></span>'+
      '<span class="vm-p" title="Home '+p[0]+'%, draw '+p[1]+'%, away '+p[2]+'%"><span class="vm-bar"><u class="h'+(x.pk===0?' pk':'')+'" style="width:'+p[0]+'%"></u><u class="d'+(x.pk===1?' pk':'')+'" style="width:'+p[1]+'%"></u><u class="a'+(x.pk===2?' pk':'')+'" style="width:'+p[2]+'%"></u></span>'+
      '<small>H '+p[0]+' · D '+p[1]+' · A '+p[2]+'</small></span>'+
      '<span class="vm-s">'+m.s[0]+'-'+m.s[1]+'</span>'+
      '<span class="vm-v '+(x.ok?'ok':'no')+'">'+(x.ok?'Called it':'Missed')+'<small>picked '+LAB[x.pk]+', was '+LAB[x.o]+'</small></span></div>';}).join('')+'</div>';
}
var CSS='.vm-t{display:flex;flex-direction:column;background:#fff;border:1px solid var(--line);border-radius:20px;overflow:hidden}'+
'.vm-r{display:grid;grid-template-columns:56px minmax(0,1.3fr) minmax(0,1.2fr) 52px 112px;gap:16px;align-items:center;padding:14px 20px;border-top:1px solid var(--line);font-size:14.5px}'+
'.vm-r:first-child{border-top:none}'+
'.vm-d{font:500 12px "JetBrains Mono",monospace;color:var(--muted)}'+
'.vm-f{display:flex;gap:7px;align-items:baseline;flex-wrap:wrap}.vm-f b{font-weight:700}.vm-f i{font-style:normal;color:var(--muted);font-size:12px}'+
'.vm-p small{display:block;margin-top:5px;font:500 11px "JetBrains Mono",monospace;color:var(--muted)}'+
'.vm-bar{display:flex;height:10px;border-radius:99px;overflow:hidden;background:var(--line);gap:2px}'+
'.vm-bar u{display:block;opacity:.32;text-decoration:none}.vm-bar u.pk{opacity:1}'+
'.vm-bar .h{background:var(--ink)}.vm-bar .d{background:#8A91A0}.vm-bar .a{background:var(--accent)}'+
'.vm-s{font:700 15px "JetBrains Mono",monospace;text-align:center;background:var(--bg-alt);border-radius:8px;padding:5px 0}'+
'.vm-v{font-weight:700;font-size:13.5px}.vm-v small{display:block;font:500 11px "JetBrains Mono",monospace;color:var(--muted);margin-top:2px}'+
'.vm-v.ok{color:#1d4ed8}.vm-v.no{color:var(--muted)}'+
'.vm-empty{background:#fff;border:1.5px dashed var(--line-strong);border-radius:20px;padding:36px 24px;text-align:center;color:var(--muted)}'+
'@media(max-width:760px){.vm-r{grid-template-columns:48px 1fr auto;row-gap:10px;column-gap:12px;padding:14px 16px}.vm-p{grid-column:1/-1;order:5}.vm-v{grid-column:1/-1;order:6;display:flex;gap:10px;align-items:baseline}.vm-v small{margin:0}}';
function injectCSS(){if(document.getElementById('vm-css'))return;var s=document.createElement('style');s.id='vm-css';s.textContent=CSS;document.head.appendChild(s);}

w.VantageModel={load:load,summarise:summarise,kpis:kpis,tableHTML:tableHTML,injectCSS:injectCSS,fromPreds:fromPreds};
})(window);
