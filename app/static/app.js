const $ = (id) => document.getElementById(id);
const rub = (v) => v == null ? '—' : new Intl.NumberFormat('ru-RU', {style:'currency', currency:'RUB', maximumFractionDigits:2}).format(v);
const asDate = (v) => new Date(v && (v.endsWith('Z') || v.includes('+')) ? v : `${v}Z`);
const when = (v) => v ? asDate(v).toLocaleString('ru-RU') : 'ещё не проверялся';
let goods = [];

async function api(path, options={}) {
  const response = await fetch(path, {headers:{'Content-Type':'application/json'},...options});
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) {
    let msg = data.detail || `HTTP ${response.status}`;
    if (Array.isArray(msg)) msg = msg.map(x=>`${(x.loc || []).slice(1).join('.')} — ${x.msg}`).join('; ');
    throw new Error(msg);
  }
  return data;
}
function message(text, isError=false) {
  $('message').hidden = false;
  $('message').textContent = text;
  $('message').classList.toggle('error', isError);
}
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
function action(text, fn, cls='') {
  const b=el('button',cls,text);
  b.type='button'; b.addEventListener('click',fn); return b;
}
const kindLabel = (kind) => kind === 'card' ? 'По карте OZON' : 'С другими банками';
const signedRub = (value) => value == null ? '—' : `${value > 0 ? '+' : value < 0 ? '−' : ''}${rub(Math.abs(value))}`;
const signedPercent = (value) => value == null ? '' : ` (${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toLocaleString('ru-RU',{maximumFractionDigits:1})}%)`;
const trendClass = (value) => value < 0 ? 'down' : value > 0 ? 'up' : 'flat';
const trendIcon = (value) => value < 0 ? '↘' : value > 0 ? '↗' : '→';
function statBox(label, value, detail='') {
  const box=el('div','stat-cell');
  box.append(el('span','stat-label',label));
  box.append(el('strong','stat-value',value));
  if(detail)box.append(el('small','stat-detail',detail));
  return box;
}
function compactAnalytics(p){
  const stats=p.analytics?.selected;
  const wrap=el('section','analytics-inline');
  const head=el('div','analytics-inline-head');
  head.append(el('span','','ДИНАМИКА С ПЕРВОЙ ЦЕНЫ'));
  if(stats)head.append(el('span','', `${stats.observations} замер${stats.observations===1?'':'ов'}`));
  wrap.append(head);
  if(!stats){wrap.append(el('div','analytics-empty','Появится после первого успешного замера'));return wrap;}
  const main=el('div','analytics-inline-main');
  const change=el('strong','analytics-change '+trendClass(stats.change_amount),
    `${trendIcon(stats.change_amount)} ${signedRub(stats.change_amount)}${signedPercent(stats.change_percent)}`);
  main.append(change);
  main.append(el('span','analytics-from', `От ${rub(stats.initial_price)} · ${when(stats.initial_at)}`));
  wrap.append(main);
  const fine=el('div','analytics-mini-grid');
  fine.append(statBox('Минимум',rub(stats.min_price)));
  fine.append(statBox('Максимум',rub(stats.max_price)));
  wrap.append(fine);
  if(p.last_error)wrap.append(el('div','analytics-stale','⚠ Динамика по последней успешной цене'));
  return wrap;
}
function makeCard(p) {
  const card=el('article','product-card');
  const top=el('div','product-top');
  top.append(el('span','marketplace','OZON'));
  let statusText = p.paused ? '⏸ На паузе' : p.last_error ? 'Ошибка проверки' : p.available === false ? 'Нет в наличии' : p.current_price == null ? 'Ожидает проверки' : p.current_price <= p.target_price && p.price_verified ? '● Цель достигнута' : !p.price_verified ? '⚠ Уточняется тип цены' : '● Отслеживается';
  const status=el('span','status'+(p.last_error?' error':p.available===false?' unavailable':''),statusText);
  top.append(status); card.append(top);
  if(p.image_url){
    const image=el('img','product-image');
    image.src=p.image_url;image.loading='lazy';image.alt='Фото товара Ozon';image.referrerPolicy='no-referrer';
    image.onerror=()=>image.remove();
    card.append(image);
  }
  card.append(el('h3','item-name',p.name));
  card.append(el('div','sku',`Артикул: ${p.sku} · ${kindLabel(p.price_kind)}`));
  const row=el('div','price-row');
  const left=el('div'); left.append(el('small','', 'Текущая цена'));
  left.append(el('strong',p.current_price!=null && p.available!==false ? (p.current_price<=p.target_price&&p.price_verified?'threshold':''):'muted',rub(p.available===false?null:p.current_price)));
  const right=el('div'); right.append(el('small','', 'Ваша цель'));
  right.append(el('strong','target',rub(p.target_price))); row.append(left,right); card.append(row);
  const prices=el('div','dual-prices');
  const priceOptions=[
    ['card','По карте OZON',p.current_card_price],
    ['regular','С другими банками',p.current_regular_price]
  ];
  priceOptions.forEach(([kind,label,value])=>{
    const item=el('div','dual-price'+(p.price_kind===kind?' selected':''));
    item.append(el('small','',label));
    item.append(el('strong',value==null?'muted':'',rub(p.available===false?null:value)));
    if(p.price_kind===kind)item.append(el('span','tracked-label','Отслеживается'));
    prices.append(item);
  });
  card.append(prices);
  card.append(compactAnalytics(p));
  const actions=el('div','card-actions');
  const link=el('a','', '↗ Товар'); link.href=p.url; link.target='_blank'; link.rel='noopener noreferrer'; actions.append(link);
  actions.append(action('↻ Проверить',async (ev)=>{
    ev.currentTarget.disabled=true;
    try {await api(`/api/products/${p.id}/check`,{method:'POST'});await load();}
    catch(err){message(err.message,true);await load();}
  }));
  actions.append(action('📈 Аналитика',()=>openHistory(p)));
  actions.append(action(p.paused?'▶ Возобновить':'⏸ Пауза',async ev=>{
    ev.currentTarget.disabled=true;
    try{await api(`/api/products/${p.id}/pause`,{method:'PATCH',body:JSON.stringify({paused:!p.paused})});await load();}
    catch(err){message(err.message,true);await load();}
  }));
  actions.append(action('⚙ Цель',async ()=>{
    const answer=prompt('Новая целевая цена (₽)',p.target_price);
    if(answer==null)return;
    const val=Number(answer.replace(',','.'));
    if(!Number.isFinite(val)||val<=0){message('Введите положительную цену',true);return;}
    try {await api(`/api/products/${p.id}`,{method:'PATCH',body:JSON.stringify({target_price:val})});await load();}
    catch(err){message(err.message,true);}
  }));
  actions.append(action('Удалить',async ()=>{
    if(!confirm('Удалить товар и историю его цен?'))return;
    try {await api(`/api/products/${p.id}`,{method:'DELETE'});await load();}
    catch(err){message(err.message,true);}
  },'danger'));
  card.append(actions);
  const foot=el('div','product-foot');
  foot.append(el('span','',`Проверено: ${when(p.last_checked_at)}`));
  foot.append(el('span','',p.last_error?'Последняя успешная цена сохранена':`Актуально на ${when(p.last_success_at)}`));
  card.append(foot);
  if(p.failure_count>0)foot.append(el('span','',`Ошибок подряд: ${p.failure_count}`));
  if(p.current_price!=null && !p.price_verified){
    card.append(el('div','analytics-stale','⚠ Цена из разметки без подтверждённого типа банковской скидки. Для уведомления нужна проверенная цена.'));
  }
  if(p.last_error)card.append(el('div','error-text',p.last_error));
  if(p.last_notify_error && !p.last_notify_error.startsWith('Telegram не настроен'))card.append(el('div','error-text',`Telegram: ${p.last_notify_error}`));
  return card;
}
function render(){
  $('count').textContent=goods.length;
  $('below').textContent=goods.filter(x=>!x.paused && x.current_price!=null && x.available!==false && !x.last_error && x.price_verified && x.current_price<=x.target_price).length;
  $('checked').textContent=goods.filter(x=>x.last_success_at && x.available!==false).length;
  const textFilter=$('search-products').value.trim().toLocaleLowerCase('ru-RU');
  const filter=$('filter-products').value;
  let items=goods.filter(p=>{
    if(textFilter && !`${p.name} ${p.sku}`.toLocaleLowerCase('ru-RU').includes(textFilter))return false;
    if(filter==='paused')return p.paused;
    if(filter==='active')return !p.paused;
    if(filter==='errors')return !!p.last_error;
    if(filter==='below')return !p.paused && p.price_verified && p.current_price!=null && p.current_price<=p.target_price && !p.last_error;
    return true;
  });
  const sort=$('sort-products').value;
  if(sort==='discount')items.sort((a,b)=>(a.analytics?.selected?.change_percent??Infinity)-(b.analytics?.selected?.change_percent??Infinity));
  else if(sort==='price')items.sort((a,b)=>(a.current_price??Infinity)-(b.current_price??Infinity));
  else if(sort==='errors')items.sort((a,b)=>Number(!!b.last_error)-Number(!!a.last_error));
  else items.sort((a,b)=>b.id-a.id);
  const dest=$('products');dest.replaceChildren();
  if(!items.length){
    const empty=el('div','empty','');
    empty.append(el('strong','','Нет подходящих товаров'));
    empty.append(el('span','','Добавьте товар или измените фильтры.'));
    dest.append(empty);return;
  }
  items.forEach(p=>dest.append(makeCard(p)));
}
async function load(){
  try {goods=await api('/api/products');render();$('server-status').textContent='● Сервер работает';$('server-status').classList.remove('bad');}
  catch(err){$('server-status').textContent='● Нет соединения';$('server-status').classList.add('bad');message(err.message,true);}
}
$('add-form').addEventListener('submit',async ev=>{
  ev.preventDefault(); $('add-button').disabled=true;
  const data={link:$('link').value.trim(),price_kind:$('price-kind').value,
    target_price:Number($('target').value)};
  message('Добавляем товар и проверяем цену. Это может занять некоторое время.');
  try {const p=await api('/api/products',{method:'POST',body:JSON.stringify(data)});
    message(p.last_error?'Товар добавлен, но цену пока получить не удалось: '+p.last_error:'Товар добавлен и первая цена сохранена.');
    $('add-form').reset();await load();
  }catch(err){message(err.message,true);}finally{$('add-button').disabled=false;}
});
$('refresh').addEventListener('click',load);
$('close-history').addEventListener('click',()=>$('history-dialog').close());
async function openHistory(p){
  $('history-title').textContent=p.name;
  $('chart-subtitle').textContent='История успешных проверок · две цены Ozon. Данные получены из витрины; персональные предложения могут отличаться.';
  const dialog=$('history-dialog'); dialog.showModal();
  $('history-analytics').replaceChildren(el('div','analytics-empty','Загружаем аналитику…'));
  $('chart-tooltip').hidden=true;
  try{
    const [data, points]=await Promise.all([
      api(`/api/products/${p.id}/analytics`),
      api(`/api/products/${p.id}/history?limit=3000`),
    ]);
    renderHistoryAnalytics(data, p);
    requestAnimationFrame(()=>drawChart(points,p.target_price,p.price_kind));
  }catch(err){$('chart-subtitle').textContent=err.message;}
}
function renderHistoryAnalytics(data,p){
  const root=$('history-analytics');root.replaceChildren();
  [['card',data.card],['regular',data.regular]].forEach(([kind,info])=>{
    const panel=el('section','history-stats-panel'+(p.price_kind===kind?' selected':''));
    const head=el('div','history-panel-title');
    head.append(el('strong','',kindLabel(kind)));
    if(p.price_kind===kind)head.append(el('span','tracked-label','Уведомления по этой цене'));
    panel.append(head);
    if(!info){panel.append(el('p','analytics-empty','Нет сохранённых замеров этой цены. История начнётся с первой успешной проверки.'));root.append(panel);return;}
    const diff=el('div','history-diff');
    diff.append(el('span','','Изменение от первой успешной цены'));
    diff.append(el('strong','analytics-change '+trendClass(info.change_amount),
      `${trendIcon(info.change_amount)} ${signedRub(info.change_amount)}${signedPercent(info.change_percent)}`));
    panel.append(diff);
    const grid=el('div','history-stats-grid');
    grid.append(statBox('Первая цена',rub(info.initial_price),when(info.initial_at)));
    grid.append(statBox('Последняя цена',rub(info.latest_price),when(info.latest_at)));
    grid.append(statBox('Минимум',rub(info.min_price)));
    grid.append(statBox('Максимум',rub(info.max_price)));
    grid.append(statBox('Средняя по замерам',rub(info.average_price)));
    grid.append(statBox('Средняя по времени*',rub(info.time_weighted_average_price)));
    grid.append(statBox('Макс. снижение от первой',rub(info.max_discount_from_initial)));
    grid.append(statBox('Дата минимальной цены',when(info.min_at)));
    if(info.below_target_estimated_hours!=null)grid.append(statBox('Часов ниже цели*',`${info.below_target_estimated_hours} ч`));
    grid.append(statBox('Проверок с ценой',String(info.observations)));
    panel.append(grid);
    const periods=el('div','period-changes');
    [['За 24 часа',info.change_24h],['За 7 дней',info.change_7d]].forEach(([label,window])=>{
      const item=el('div','period-item');
      item.append(el('span','',label));
      const val=window ? `${signedRub(window.amount)}${signedPercent(window.percent)}` : 'Недостаточно истории';
      item.append(el('strong',window?trendClass(window.amount):'muted',val));
      periods.append(item);
    });
    panel.append(periods);
    root.append(panel);
  });
  root.append(el('p','form-help','* При расчёте по времени предполагается, что цена сохранялась между проверками. Это оценка, а не непрерывное наблюдение.'));
  if(p.last_error)root.append(el('p','analytics-stale','⚠ Последняя проверка завершилась ошибкой. Аналитика основана на последней успешной цене.'));
}
function drawChart(points,target,selectedKind){
  const canvas=$('price-chart'),frame=canvas.parentElement,tip=$('chart-tooltip');
  const r=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;
  const w=Math.max(300,r.width),h=300;
  canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);
  const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
  const valid=points.flatMap(p=>[p.card_price,p.regular_price].filter(v=>v!=null));
  if(!valid.length){ctx.font='14px sans-serif';ctx.textAlign='center';ctx.fillStyle='#8da1c3';ctx.fillText('История появится после успешной проверки',w/2,h/2);canvas.onmousemove=null;return;}
  const lo=Math.min(...valid,target),hi=Math.max(...valid,target),span=Math.max(1,hi-lo),low=Math.max(0,lo-span*.12),high=hi+span*.12;
  const l=62,t=25,right=21,b=37,pw=w-l-right,ph=h-t-b;
  const times=points.map(p=>asDate(p.checked_at).getTime());
  const first=Math.min(...times),last=Math.max(...times),range=Math.max(1,last-first);
  const x=i=>l+(last===first?pw/2:(times[i]-first)/range*pw);
  const y=v=>t+(high-v)/(high-low)*ph;
  ctx.font='11px sans-serif';ctx.strokeStyle='#253954';ctx.fillStyle='#8397b8';ctx.lineWidth=1;ctx.textAlign='right';
  for(let i=0;i<=4;i++){const v=low+i*(high-low)/4,py=y(v);ctx.beginPath();ctx.moveTo(l,py);ctx.lineTo(w-right,py);ctx.stroke();ctx.fillText(Math.round(v).toLocaleString('ru-RU'),l-7,py+4);}
  ctx.strokeStyle='#d2b178';ctx.lineWidth=1.3;ctx.setLineDash([5,5]);ctx.beginPath();ctx.moveTo(l,y(target));ctx.lineTo(w-right,y(target));ctx.stroke();ctx.setLineDash([]);
  function plot(kind,color){
    let has=false;ctx.strokeStyle=color;ctx.lineWidth=kind===selectedKind?3:2.1;ctx.beginPath();
    points.forEach((p,i)=>{const v=p[kind+'_price'];if(v==null){has=false;return;}
      if(!has)ctx.moveTo(x(i),y(v));else ctx.lineTo(x(i),y(v));has=true;});ctx.stroke();
    points.forEach((p,i)=>{const v=p[kind+'_price'];if(v==null || (points.length>85 && i%Math.ceil(points.length/65)!==0 && i!==points.length-1))return;
      ctx.beginPath();ctx.arc(x(i),y(v),points.length>50?2:3.3,0,2*Math.PI);ctx.fillStyle=color;ctx.fill();});
  }
  plot('card','#62dfac');plot('regular','#8aa7ff');
  ctx.fillStyle='#8397b8';ctx.font='11px sans-serif';ctx.textAlign='left';ctx.fillText(asDate(points[0].checked_at).toLocaleDateString('ru-RU'),l,h-8);
  ctx.textAlign='right';ctx.fillText(asDate(points[points.length-1].checked_at).toLocaleDateString('ru-RU'),w-right,h-8);
  canvas.onmousemove=(event)=>{
    const bounds=canvas.getBoundingClientRect(),mouseX=event.clientX-bounds.left;
    if(mouseX<l || mouseX>w-right){tip.hidden=true;return;}
    let idx=0,best=Infinity;times.forEach((v,i)=>{const d=Math.abs(x(i)-mouseX);if(d<best){best=d;idx=i;}});
    const p=points[idx];tip.textContent='';
    tip.append(el('strong','',when(p.checked_at)));
    tip.append(el('span','',`По карте OZON: ${rub(p.card_price)}`));
    tip.append(el('span','',`С другими банками: ${rub(p.regular_price)}`));
    if(p.price_verified===false)tip.append(el('span','', '⚠ Тип цены не подтверждён'));
    tip.style.left=`${Math.min(Math.max(mouseX+12,8),w-205)}px`;
    tip.style.top='12px';tip.hidden=false;
  };
  canvas.onmouseleave=()=>{tip.hidden=true;};
}

['search-products','sort-products','filter-products'].forEach(id=>$(id).addEventListener(id==='search-products'?'input':'change',render));
load();
async function loadHealth(){
  try {
    const d=await api('/api/health');
    $('interval').textContent=d.interval_minutes+' мин';
    $('next-check').textContent=d.next_check_at?`Следующая: ${when(d.next_check_at)}`:'Планировщик остановлен';
  }catch(e){console.warn('Нет статуса планировщика',e.message);}
}
loadHealth();
