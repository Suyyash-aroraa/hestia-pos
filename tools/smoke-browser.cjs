const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const assert = require('assert/strict');
const adminPassword = require('crypto').randomBytes(32).toString('hex');
const root = path.resolve(__dirname, '..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hestia-browser-'));
const python = process.env.PYTHON || (fs.existsSync(path.join(root,'.venv','Scripts','python.exe')) ? path.join(root,'.venv','Scripts','python.exe') : path.join(root,'..','.venv','Scripts','python.exe'));
const base = 'http://127.0.0.1:5011';
const artifacts = path.join(root,'artifacts');
fs.mkdirSync(artifacts,{recursive:true});
const server=spawn(python,['-c', `from app import app\nfrom models import db, MenuItem\nwith app.app_context():\n db.session.add(MenuItem(name='Coffee', category='Drinks', price=100, available=True))\n db.session.commit()\nfrom waitress import serve\nserve(app, host='127.0.0.1', port=5011, threads=16)`], {
  cwd:root, windowsHide:true,
  env:{...process.env, HESTIA_ADMIN_PASSWORD:adminPassword, HESTIA_DATA_DIR:tmp, HESTIA_DATABASE_URL:'sqlite:///'+path.join(tmp,'test.db').replaceAll('\\','/'), HESTIA_TESTING:'1', HESTIA_PORT:'5011'},
});
let serverLog=''; server.stdout.on('data',d=>serverLog+=d);server.stderr.on('data',d=>serverLog+=d);
let browser;
(async()=>{
 try {
  let ready=false;
  for(let i=0;i<80;i++){try{const r=await fetch(base+'/api/config');if(r.ok){ready=true;break;}}catch{} await new Promise(r=>setTimeout(r,250));}
  assert(ready,serverLog);
  browser=await chromium.launch({channel:'chrome',headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  const errors=[]; const failed=[];
  context.on('page',page=>{
    page.on('pageerror',err=>{errors.push(page.url()+': '+err.message);console.error('PAGE ERROR:',err.message);});
    page.on('response',r=>{if(r.status()>=500)failed.push(r.url()+': '+r.status());});
  });
  for(const route of ['/','/pos','/pos-takeout','/menu-manager','/history','/items-report','/admin']){
    const page=await context.newPage();
    await page.goto(base+route); await page.waitForTimeout(650);
    if(route==='/') await page.screenshot({path:path.join(artifacts,'dashboard.png'),fullPage:true});
    if(route==='/admin'){
      await page.locator('input[x-model="password"]').fill(adminPassword);
      await page.getByRole('button',{name:'Login',exact:true}).click();
      await page.waitForTimeout(800);
      assert(!await page.locator('.login-card').isVisible(), 'Admin login failed');
    }
    if(route==='/pos'||route==='/pos-takeout'){
      await page.locator(route==='/pos'?'.side-table-btn':'.slot-bar-btn').first().click();
      await page.getByRole('button',{name:route==='/pos'?'Open table':'Open parcel',exact:true}).click();
      await page.locator('.menu-item-card').filter({hasText:'Coffee'}).first().waitFor();
      await page.locator('.menu-item-card').filter({hasText:'Coffee'}).first().click();
      await page.locator('[x-show="qtyModal.show"] button').filter({hasText:/^OK$/}).click();
      await page.waitForFunction(()=>Alpine.$data(document.body).staffCart.length===1);
      await page.evaluate(()=>Alpine.$data(document.body).submitStaffOrder());
      await page.waitForFunction(()=>Alpine.$data(document.body).staffCart.length===0);
      await page.waitForFunction(()=>Alpine.$data(document.body).previewGrandTotal===100);
      // Capture the bill payload without sending a job to an installed printer.
      await page.evaluate(()=>window.printBillReceipt=(data)=>{window.__testReceipt=data;});
      await page.locator('button.panel-tab').filter({hasText:/^Bill$/}).click();
      await page.locator('button.act-btn-print').click();
      await page.waitForFunction(()=>!!window.__testReceipt);
      const receipt=await page.evaluate(()=>window.__testReceipt);
      assert.equal(receipt.total,100);
      assert(!('service_charge' in receipt));
      await page.screenshot({path:path.join(artifacts,route.slice(1)+'.png'),fullPage:true});
    }
    console.log('Browser passed:',route);
    await page.close();
  }
  assert.deepEqual(failed,[],'Server failures');
  assert.deepEqual(errors,[],'Browser runtime errors');
  console.log('Browser screens, cart entry, bill generation and admin login passed.');
 } finally {
  if(browser)await browser.close();
  server.kill();
 }
})().catch(err=>{console.error(err);console.error(serverLog.slice(-6000));process.exitCode=1;});
