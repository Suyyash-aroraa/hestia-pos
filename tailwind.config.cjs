module.exports = {
  content: [
    './frontend/**/*.{html,js}',
  ],
  safelist: [
    'text-amber-400',
    'text-slate-300',
  ],
  theme: {
    extend: {},
  },
  corePlugins: {
    preflight: true,
  },
};
