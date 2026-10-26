// Read-only baseline replay: use saved pre-pass production files with exactly
// the same fixtures/assertions. Normal test runs use the working tree.
import {pathToFileURL} from 'node:url';
import {readFileSync} from 'node:fs';
const root=process.env.A4_RENDER_ROOT ? pathToFileURL(process.env.A4_RENDER_ROOT.replace(/\/$/,'')+'/') : new URL('../',import.meta.url);
export const layout=await import(new URL('web/map-layout.mjs',root));
export const onlineMode=await import(new URL('web/online-code-mode.mjs',root));
export const appDefinitions=readFileSync(new URL('web/app.js',root),'utf8');
