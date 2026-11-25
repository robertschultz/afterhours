import {build} from 'esbuild';
await build({entryPoints:['src/afterhours/web/director-sdk.js'],bundle:true,format:'esm',platform:'browser',target:['safari17','chrome120'],outfile:'src/afterhours/web/vendor/director-sdk.js',legalComments:'linked',minify:true});
