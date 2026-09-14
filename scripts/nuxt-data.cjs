// Evaluate only the issuer's serialized Nuxt state in a context without host APIs.
const vm = require('node:vm');
const fs = require('node:fs');
const html = fs.readFileSync(0, 'utf8');
const script = html.match(/<script>window\.__NUXT__=([\s\S]*?)<\/script>/);
if (!script) throw new Error('Missing Nuxt state');
const context = vm.createContext(Object.create(null), {
  codeGeneration: { strings: false, wasm: false }
});
const json = vm.runInContext(`JSON.stringify(${script[1].replace(/;\s*$/, '')})`, context, { timeout: 3000 });
process.stdout.write(json);
