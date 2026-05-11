/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        'bg-base': '#0a0f1e',
        'bg-surface': '#111827',
        'bg-elevated': '#1a2235',
        'border-custom': '#1f2937',
        'text-primary': '#f9fafb',
        'text-muted': '#6b7280',
        'text-dim': '#374151',
        'trade-green': '#10b981',
        'trade-yellow': '#f59e0b',
        'trade-red': '#ef4444',
        'trade-blue': '#3b82f6',
        'trade-orange': '#f97316',
        'trade-purple': '#8b5cf6',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'monospace'],
      },
    },
  },
  plugins: [],
}