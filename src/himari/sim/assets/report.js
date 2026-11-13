(function(){
  const data = JSON.parse(document.getElementById('report-data').textContent);
  function tip(card){let t=card.querySelector('.tip');if(!t){t=document.createElement('div');t.className='tip';card.appendChild(t);}return t;}
  function row(t,color,name,value){const r=document.createElement('div');r.className='row';const a=document.createElement('span');
    const k=document.createElement('i');k.className='key';k.style.background=color;a.appendChild(k);a.appendChild(document.createTextNode(name));
    const b=document.createElement('b');b.textContent=value;r.appendChild(a);r.appendChild(b);t.appendChild(r);}
  function place(card,t,evt,svg){const c=card.getBoundingClientRect();let x=evt.clientX-c.left+14,y=evt.clientY-c.top+10;
    t.style.display='block';if(x+t.offsetWidth>c.width)x=evt.clientX-c.left-t.offsetWidth-14;t.style.left=x+'px';t.style.top=y+'px';}
  const css=getComputedStyle(document.querySelector('.viz-root'));
  // NAV chart: the crosshair finds the date; one tooltip lists every series
  const nav=document.getElementById('nav-chart');
  if(nav){const card=nav.parentElement,cross=document.getElementById('nav-cross'),hit=nav.querySelector('.hit'),t=tip(card);
    const n=data.nav.days.length,x0=+hit.getAttribute('x'),w=+hit.getAttribute('width');let idx=n-1;
    function show(i,evt){idx=Math.max(0,Math.min(n-1,i));const vb=nav.viewBox.baseVal,sx=x0+w*idx/Math.max(n-1,1);
      cross.setAttribute('x1',sx);cross.setAttribute('x2',sx);cross.setAttribute('visibility','visible');
      t.replaceChildren();const d=document.createElement('div');d.className='date';d.textContent=data.nav.days[idx];t.appendChild(d);
      data.nav.series.forEach(s=>{const v=s.values[idx];row(t,css.getPropertyValue(s.var),s.name,v==null?'n/a':v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})+' CAD');});
      const r=nav.getBoundingClientRect();const e=evt||{clientX:r.left+r.width*sx/vb.width,clientY:r.top+r.height*0.3};place(card,t,e,nav);}
    nav.addEventListener('pointermove',e=>{const r=nav.getBoundingClientRect(),vb=nav.viewBox.baseVal;
      const sx=(e.clientX-r.left)*vb.width/r.width;show(Math.round((sx-x0)/w*(n-1)),e);});
    nav.addEventListener('pointerleave',()=>{t.style.display='none';cross.setAttribute('visibility','hidden');});
    nav.addEventListener('keydown',e=>{if(e.key==='ArrowLeft'){show(idx-1);e.preventDefault();}if(e.key==='ArrowRight'){show(idx+1);e.preventDefault();}});
    nav.addEventListener('focus',()=>show(idx));nav.addEventListener('blur',()=>{t.style.display='none';cross.setAttribute('visibility','hidden');});}
  // weights chart: each column is its own hit target
  const wc=document.getElementById('w-chart');
  if(wc){const card=wc.parentElement,t=tip(card);
    wc.querySelectorAll('.col').forEach(c=>{c.addEventListener('pointermove',e=>{const i=+c.dataset.i;t.replaceChildren();
      const d=document.createElement('div');d.className='date';d.textContent=data.weights.days[i];t.appendChild(d);
      let sum=0;data.weights.symbols.forEach((s,k)=>{const v=data.weights.values[i][k];sum+=v;if(v>0)row(t,css.getPropertyValue('--s'+(k+1)),s,(v*100).toFixed(1)+'%');});
      row(t,'transparent','Cash',((1-sum)*100).toFixed(1)+'%');place(card,t,e,wc);});
      c.addEventListener('pointerleave',()=>{t.style.display='none';});});}
})();
