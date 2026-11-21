'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');
const TYPE="analytic";
const IDS=["unemploymentTotal","unemploymentRate","unemploymentType","longterm","vacancies","wages","notices","bankruptcies","expiredBenefits","workSharing","failedRecruitment","topRecruitmentOccupations","confidence","business","inflation","eurostatTrend","eurostatLatest","euConfidence"];
const BUTTON=TYPE==='analytic'?'.mk-linkedin-buttons .mk-share-linkedin':'.mk-social-actions .mk-linkedin-btn';
const LINK=TYPE==='analytic'?'.mk-linkedin-buttons .mk-share-copy':'.mk-social-actions .mk-copy-btn';
const ROOT='http://127.0.0.1:8765/';
const sharePage=id=>TYPE==='analytic'?'share/'+id+'/index.html':'share/'+id+'/index.html';
const imagePath=id=>TYPE==='analytic'?'share/'+id+'.png':'assets/share-previews/'+id+'.png';
(async()=>{
 for(const id of IDS){
  const p=sharePage(id);
  assert(fs.existsSync(p),'Mangler '+p);
  const html=fs.readFileSync(p,'utf8');
  assert(html.includes('og:image')&&html.includes('Michel Klos'),'OG metadata/afsender mangler: '+id);
  assert(fs.existsSync(imagePath(id)),'Mangler LinkedIn-billede '+id);
  assert(fs.statSync(imagePath(id)).size>3000,'Billedet er for lille: '+id);
 }
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1200,height:900}});
 try{
  await page.goto(ROOT,{waitUntil:'domcontentloaded',timeout:45000});
  await page.waitForFunction(({n,selector})=>document.querySelectorAll(selector).length===n,
    {n:IDS.length,selector:BUTTON},{timeout:55000});
  const buttons=await page.locator(BUTTON).count(),copies=await page.locator(LINK).count();
  assert.equal(buttons,IDS.length,'Antal LinkedIn-knapper');
  assert.equal(copies,IDS.length,'Antal kopiér-link-knapper');
  if(TYPE==='analytic')await page.locator('#dak-dashboard [data-months="12"]').click();
  if(TYPE==='senior')await page.selectOption('#age','67-74');
  await page.evaluate(()=>{window.__testSharedUrl=null;window.open=(url)=>{window.__testSharedUrl=url;return null;};});
  await page.locator(BUTTON).first().click();
  const link=await page.evaluate(()=>window.__testSharedUrl);
  assert(link&&link.includes('linkedin.com/sharing/share-offsite'),'LinkedIn popup link mangler');
  const destination=new URL(new URL(link).searchParams.get('url'));
  assert(destination.pathname.includes('/share/'+IDS[0]+'/'),'Deleside matcher ikke grafen');
  if(TYPE==='analytic')assert.equal(destination.searchParams.get('months'),'12','Valgt periode blev ikke gemt');
  if(TYPE==='senior')assert.equal(destination.searchParams.get('age'),'67-74','Valgt alder blev ikke gemt');
  console.log('Share buttons and filters OK:',TYPE,buttons,'LinkedIn destination:',destination.href);
  const focus=TYPE==='analytic'?'?months=12#graf-unemploymentTotal':TYPE==='senior'?'?age=67-74&figure=age-chart':'?figure=timeChart';
  await page.goto(ROOT+focus,{waitUntil:'domcontentloaded',timeout:45000});
  const focusSelector=TYPE==='analytic'?'#graf-unemploymentTotal.share-target':TYPE==='senior'?'#age-chart':'#timeChart';
  if(TYPE==='analytic'){
    await page.waitForSelector(focusSelector,{timeout:25000});
    const active=await page.locator('#dak-dashboard [data-months="12"]').getAttribute('class');
    assert(active.includes('active'),'12 måneders periode blev ikke genskabt');
  }else{
    await page.waitForSelector('.mk-share-highlight',{timeout:55000});
    const highlighted=await page.locator('.mk-share-highlight').locator(focusSelector).count();
    assert.equal(highlighted,1,'Den rette graf er ikke fremhævet');
  }
  console.log('Deep link and selected-view restoration OK:',TYPE);
 }finally{await browser.close();}
})().catch(err=>{console.error(err);process.exitCode=1;});
