/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./src/clientcloak/ui/templates/**/*.html",
    "./src/clientcloak/ui/static/**/*.js",
  ],
  theme: {
    extend: {
      colors: {
        navy: {
          DEFAULT: '#1a1a2e',
          50: '#e8e8ed',
          100: '#d1d1db',
          200: '#a3a3b7',
          300: '#757593',
          400: '#47476f',
          500: '#1a1a2e',
          600: '#151525',
          700: '#10101c',
          800: '#0b0b13',
          900: '#06060a',
        },
        teal: {
          DEFAULT: '#00d9ff',
          50: '#e6fbff',
          100: '#b3f3ff',
          200: '#80ecff',
          300: '#4de4ff',
          400: '#1addff',
          500: '#00d9ff',
          600: '#00aecc',
          700: '#008299',
          800: '#005766',
          900: '#002b33',
        },
        success: '#4ade80',
        warning: '#fbbf24',
        danger: '#ef4444',
        offwhite: '#fafafa',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['JetBrains Mono', 'Menlo', 'monospace'],
      },
      animation: {
        'cloak-pulse': 'cloakPulse 2s ease-in-out infinite',
        'cloak-spin': 'cloakSpin 1.5s linear infinite',
        'slide-in': 'slideIn 0.4s ease-out',
        'fade-in': 'fadeIn 0.3s ease-out',
        'check-pop': 'checkPop 0.3s ease-out',
      },
      keyframes: {
        cloakPulse: {
          '0%, 100%': { opacity: '1', transform: 'scale(1)' },
          '50%': { opacity: '0.7', transform: 'scale(1.05)' },
        },
        cloakSpin: {
          '0%': { transform: 'rotate(0deg)' },
          '100%': { transform: 'rotate(360deg)' },
        },
        slideIn: {
          '0%': { opacity: '0', transform: 'translateY(16px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        checkPop: {
          '0%': { transform: 'scale(0)' },
          '70%': { transform: 'scale(1.2)' },
          '100%': { transform: 'scale(1)' },
        },
      },
    },
  },
  plugins: [],
}
