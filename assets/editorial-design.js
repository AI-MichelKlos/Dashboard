(function(){
  'use strict';
  const nav=document.querySelector('.mk-cross-dashboard');
  if(nav){
    const inner=nav.querySelector('.mk-cross-inner');
    if(inner && !inner.querySelector('.mk-menu-toggle')){
      const b=document.createElement('button');
      b.type='button';b.className='mk-menu-toggle';b.textContent='Menu';
      b.setAttribute('aria-expanded','false');
      b.setAttribute('aria-label','Vis eller skjul dashboardnavigation');
      b.setAttribute('aria-controls','mk-cross-site-links');
      inner.id='mk-cross-site-links';
      const firstLink=inner.querySelector('a');
      inner.insertBefore(b,firstLink||null);
      function close(){
        nav.classList.remove('is-open');
        b.setAttribute('aria-expanded','false');
        b.textContent='Menu';
      }
      b.addEventListener('click',()=>{
        const open=nav.classList.toggle('is-open');
        b.setAttribute('aria-expanded',String(open));
        b.textContent=open?'Luk':'Menu';
      });
      nav.addEventListener('keydown',e=>{if(e.key==='Escape'){close();b.focus();}});
      inner.querySelectorAll('a').forEach(a=>a.addEventListener('click',close));
      if(window.matchMedia){
        const media=matchMedia('(max-width:760px)');
        if(media.addEventListener) media.addEventListener('change',()=>{if(!media.matches)close();});
      }
    }
  }
  // Palette opdateres på eksisterende Chart.js-figurer, uden at ændre værdier.
  const colorMap={
    '#9a5b34':'#0B72B8', '#4a3428':'#123F5B', '#146298':'#50A7D6',
    '#8c9c41':'#28A29C', '#8c7fcf':'#7B8AC9', '#e2d2bf':'#BDDCEA',
    '#6f6258':'#557082', '#e6ddd2':'#E3ECF3', '#c9bcae':'#B8CAD8',
    '#2b211b':'#17364C'
  };
  function recolor(value){
    if(Array.isArray(value))return value.map(recolor);
    if(typeof value==='string'){
      const found=colorMap[value.toLowerCase()];
      if(found)return found;
      return value.replace(/rgba?\(\s*154\s*,\s*91\s*,\s*52\s*,/gi,'rgba(11,114,184,')
        .replace(/rgba?\(\s*74\s*,\s*52\s*,\s*40\s*,/gi,'rgba(18,63,91,');
    }
    return value;
  }
  const fields=['borderColor','backgroundColor','hoverBackgroundColor','pointBackgroundColor','pointBorderColor'];
  function adjust(chart){
    if(!chart||!chart.data||!Array.isArray(chart.data.datasets))return;
    let changed=false;
    for(const d of chart.data.datasets){
      for(const key of fields){
        if(typeof d[key]==='undefined')continue;
        const next=recolor(d[key]);
        if(Array.isArray(next)){if(JSON.stringify(next)!==JSON.stringify(d[key])){d[key]=next;changed=true;}}
        else if(next!==d[key]){d[key]=next;changed=true;}
      }
    }
    if(changed){try{chart.update('none');}catch(_){}}
  }
  function refresh(){
    if(!window.Chart)return;
    Chart.defaults.color='#4F687B';
    Chart.defaults.borderColor='#E3ECF3';
    Object.values(Chart.instances||{}).forEach(adjust);
  }
  if(window.Chart && !Chart.registry.plugins.get('mk-editorial-theme')){
    Chart.register({id:'mk-editorial-theme',beforeUpdate(chart){
      for(const d of chart.data.datasets||[]){
        for(const key of fields){if(d[key]!==undefined)d[key]=recolor(d[key]);}
      }
    }});
  }
  refresh();
  let count=0;
  const timer=setInterval(()=>{refresh();if(++count>=16)clearInterval(timer);},650);
  window.addEventListener('load',refresh,{once:true});
})();