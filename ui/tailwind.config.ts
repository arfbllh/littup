import type { Config } from 'tailwindcss';

const config: Config = {
  content: [
    './pages/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
    './app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        paper: {
          50: '#fbf8f1',
          100: '#f6f1e7',
          200: '#ece5d4',
          300: '#d6cdb8',
          400: '#a89e87',
          500: '#6f6757',
          600: '#4b4538',
          700: '#2f2b22',
          800: '#1a1714',
        },
        ochre: {
          50: '#fef6e6',
          100: '#fbe5b8',
          400: '#c8861f',
          500: '#b8741a',
          600: '#985f15',
          700: '#6f4510',
        },
        cite: {
          supported: '#2f7a55',
          'supported-bg': '#e3f1ea',
          partial: '#c08c12',
          'partial-bg': '#fbf0d3',
          unsupported: '#b1442a',
          'unsupported-bg': '#f6e0d8',
          unchecked: '#6f6757',
          'unchecked-bg': '#ece5d4',
        },
        status: {
          'pending-fg': '#4b4538',
          'pending-bg': '#ece5d4',
          'running-fg': '#6f4510',
          'running-bg': '#fbe5b8',
          'ready-fg': '#2f7a55',
          'ready-bg': '#e3f1ea',
          'failed-fg': '#b1442a',
          'failed-bg': '#f6e0d8',
        },
      },
      fontFamily: {
        sans: ['var(--font-sans)', 'system-ui', 'sans-serif'],
        mono: ['var(--font-mono)', 'monospace'],
        serif: ['var(--font-serif)', 'Georgia', 'serif'],
      },
      fontSize: {
        base: ['14px', { lineHeight: '1.5' }],
      },
      borderRadius: {
        card: '6px',
        input: '5px',
        pill: '999px',
      },
    },
  },
  plugins: [],
};

export default config;
