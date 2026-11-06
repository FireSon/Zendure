import resolve from '@rollup/plugin-node-resolve';
import typescript from '@rollup/plugin-typescript';
import terser from '@rollup/plugin-terser';
import replace from '@rollup/plugin-replace';

const MINIFY = process.env.MINIFY !== '0';

const plugins = () => [
  replace({ preventAssignment: true, 'process.env.NODE_ENV': JSON.stringify('production') }),
  resolve(),
  typescript({ tsconfig: './tsconfig.json' }),
  ...(MINIFY ? [terser()] : []),
];

const bundle = (input, file) => ({
  input,
  output: { file, format: 'es', sourcemap: true, inlineDynamicImports: true },
  plugins: plugins(),
});

export default [
  bundle('src/zm-app.ts', 'dist/zm-app.js'),  // standalone (index.html)
  // HA sidebar panel ships inside the component so a real install has it next to the code.
  bundle('src/panel.ts', '../custom_components/zenmaster/frontend/ZenMaster-panel.js'),
];
