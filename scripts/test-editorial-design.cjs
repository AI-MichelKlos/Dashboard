'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');

const EXPECTED=18;
const ROOT_SELECTOR="#dak-dashboard";
const INDEX=fs.readFileSync('index.html','utf8');
assert(INDEX.includes('assets/editorial-design.css'),'Dashboard mangler design-CSS');
assert(INDEX.includes('assets/editorial-design.js'),'Dashboard mangler mobilmenu');
assert(INDEX.includes('Michel Klos<small>Arbejdsmarkedsanalyser</small>'),'Dashboardnavn mangler');
assert(!INDEX.includes('Alle dashboards</a>'),'Uønsket link til alle dashboards findes');
for(const f of ['assets/editorial-wave.svg','assets/editorial-design.css','assets/editorial-design.js']){
  assert(fs.existsSync(f),f+' mangler');
}

(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 let failures=[];
 try{
 for(const width of [1440,1024,768,390,320]){
  const page=await browser.newPage({viewport:{width,height:900},deviceScaleFactor:1});
  page.on('pageerror',e=>failures.push({width,error:e.message}));
  await page.goto('http://127.0.0.1:8765/',{waitUntil:'domcontentloaded',timeout:50000});
  await page.waitForSelector('.mk-cross-dashboard .mk-cross-label',{timeout:20000});
  await page.waitForFunction(()=>document.querySelector('link[href*="editorial-design.css"]') && document.querySelector('.mk-cross-dashboard .mk-menu-toggle'),null,{timeout:25000});
  const report=await page.evaluate(root=>{
    const nav=document.querySelector('.mk-cross-dashboard');
    const heading=document.querySelector('header h1');
    const hero=heading.closest('header');
    const labels=[...nav.querySelectorAll('a')].map(a=>a.textContent.trim());
    const link=document.querySelector('link[href*="editorial-design.css"]');
    const st=getComputedStyle(hero);
    return {
      width:innerWidth,scrollWidth:document.documentElement.scrollWidth,
      navBackground:getComputedStyle(nav).backgroundColor,heroBackground:st.backgroundColor,
      heroBgImage:st.backgroundImage,h1Color:getComputedStyle(heading).color,
      oldBannerDisplay:getComputedStyle(hero,'::before').display,
      menuVisible:getComputedStyle(nav.querySelector('.mk-menu-toggle')).display!=='none',
      links:labels,navBeforeHeader:nav.compareDocumentPosition(hero)&Node.DOCUMENT_POSITION_FOLLOWING,
      cards:document.querySelectorAll(root+' canvas').length, cssLoaded:!!link?.sheet,
      heroHeading:heading.textContent.trim()
    };
  },ROOT_SELECTOR);
  assert(report.cssLoaded,'Stylesheet not loaded at '+width);
  assert(report.navBackground==='rgb(9, 43, 69)','Navigation should be navy '+width+': '+report.navBackground);
  assert(report.heroBgImage.includes('editorial-wave.svg'),'Blue wave missing '+width+': '+report.heroBgImage);
  assert(report.heroBackground==='rgb(249, 252, 255)','Hero should be white '+width+': '+report.heroBackground);
  assert(report.oldBannerDisplay==='none','Old dark photographic overlay is still visible at '+width+': '+report.oldBannerDisplay);
  assert(report.navBeforeHeader,'Navigation should precede hero');
  assert(report.links.length===4,'Expected four dashboard links, got '+report.links);
  assert(report.cards>=EXPECTED,'Expected preserved graphs '+EXPECTED+' got '+report.cards);
  if(report.scrollWidth>report.width+2){
    const offenders=await page.evaluate(()=>Array.from(document.querySelectorAll('body *')).map(el=>{
      const r=el.getBoundingClientRect();return {tag:el.tagName,cls:String(el.className||'').slice(0,100),id:el.id,right:Math.round(r.right),left:Math.round(r.left),width:Math.round(r.width)};
    }).filter(x=>x.right>innerWidth+3&&x.width>1).sort((a,b)=>b.right-a.right).slice(0,18));
    console.error('OVERFLOW DEBUG',width,JSON.stringify(offenders));
  }
  if(report.scrollWidth>report.width+2){
    const scrollX=await page.evaluate(()=>{window.scrollTo({left:100,top:0,behavior:'instant'});return window.scrollX;});
    assert.equal(scrollX,0,'Horizontal scrolling possible at '+width);
  }
  const button=page.locator('.mk-cross-dashboard .mk-menu-toggle');
  if(width<=760){
    assert(report.menuVisible,'Mobile toggle missing '+width);
    const link=page.locator('.mk-cross-dashboard .mk-cross-inner>a').first();
    assert.equal(await link.isVisible(),false,'Mobile menu should be collapsed');
    await button.click();
    assert.equal(await button.getAttribute('aria-expanded'),'true');
    assert.equal(await link.isVisible(),true,'Menu link should appear after click');
    await button.click();
    assert.equal(await button.getAttribute('aria-expanded'),'false');
  }else assert(!report.menuVisible,'Desktop toggle should be hidden');
  console.log('PASS',JSON.stringify(report));
  await page.close();
 }
 // External API/CDN failures can occur outside the design itself; report but don't mislabel them as layout bugs.
 const relevant=failures.filter(f=>/editorial-design|mk-cross/i.test(f.error));
 assert(relevant.length===0,'Theme runtime error: '+JSON.stringify(relevant));
 console.log('All responsive editorial design checks passed. Page errors:',failures.slice(0,7));
 }finally{await browser.close();}
})().catch(err=>{console.error(err);process.exitCode=1;});
