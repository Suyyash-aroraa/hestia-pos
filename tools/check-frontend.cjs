const fs = require('fs');
const path = require('path');
const acorn = require('acorn');
const root = path.join(__dirname, '..', 'frontend');
let count=0;
function check(filename) {
  if (filename.endsWith('.min.js') || filename.endsWith('tailwindcss.js')) return;
  const src = fs.readFileSync(filename, 'utf8');
  if(filename.endsWith('.js')) { acorn.parse(src, {ecmaVersion:'latest'}); count++; }
  if(filename.endsWith('.html')) {
    for(const match of src.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) {
      if(match[1].trim()) { acorn.parse(match[1],{ecmaVersion:'latest'}); count++; }
    }
    if(/\/api\/kitchen\/|\/api\/takeout\/online-status|Special Instructions|Service Charge|Cloudflare|\/api\/payments\/create-link/i.test(src)) throw Error('Removed feature remains in '+filename);
  }
}
function visit(dir) { for(const entry of fs.readdirSync(dir,{withFileTypes:true})) {const f=path.join(dir,entry.name); if(entry.isDirectory())visit(f);else if(/\.(js|html)$/.test(f))check(f);} }
visit(root);
console.log(`Parsed ${count} frontend scripts; removed-feature scan passed.`);
