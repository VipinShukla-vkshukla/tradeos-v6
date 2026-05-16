// TradeOS Theme System Types

export interface ThemeTokens {
  // Core backgrounds
  bgDashboard: string;
  bgPanel: string;
  bgPanelHover: string;
  bgHeader: string;
  bgInput: string;
  bgModal: string;
  bgTooltip: string;

  // Text
  textPrimary: string;
  textSecondary: string;
  textMuted: string;
  textInverse: string;

  // Borders
  borderDefault: string;
  borderFocus: string;
  borderError: string;

  // Semantic colors
  accentPrimary: string;
  accentPrimaryHover: string;
  success: string;
  successBg: string;
  warning: string;
  warningBg: string;
  danger: string;
  dangerBg: string;
  info: string;
  infoBg: string;

  // Trading-specific
  profitGreen: string;
  lossRed: string;
  bullish: string;
  bearish: string;
  neutral: string;

  // Chart palette (8 colors)
  chart1: string;
  chart2: string;
  chart3: string;
  chart4: string;
  chart5: string;
  chart6: string;
  chart7: string;
  chart8: string;

  // Component-specific
  skeletonBase: string;
  skeletonShimmer: string;
  badgeBg: string;
  scrollbarThumb: string;
  scrollbarTrack: string;

  // Radius
  radiusSm: string;
  radiusMd: string;
  radiusLg: string;
}

export interface ThemePreset {
  id: string;
  name: string;
  tokens: ThemeTokens;
}

export interface ThemeState {
  currentPresetId: string;
  customTokens: Partial<ThemeTokens>;
  presets: ThemePreset[];
}

// 5 Built-in Theme Presets
export const THEME_PRESETS: ThemePreset[] = [
  {
    id: 'midnight-navy',
    name: 'Midnight Navy',
    tokens: {
      bgDashboard: '#0a0f1a',
      bgPanel: '#111827',
      bgPanelHover: '#1a2332',
      bgHeader: '#0d1321',
      bgInput: '#1e293b',
      bgModal: '#151f2e',
      bgTooltip: '#1e293b',
      textPrimary: '#f8fafc',
      textSecondary: '#94a3b8',
      textMuted: '#64748b',
      textInverse: '#0f172a',
      borderDefault: '#1e293b',
      borderFocus: '#3b82f6',
      borderError: '#ef4444',
      accentPrimary: '#3b82f6',
      accentPrimaryHover: '#2563eb',
      success: '#22c55e',
      successBg: 'rgba(34, 197, 94, 0.1)',
      warning: '#f59e0b',
      warningBg: 'rgba(245, 158, 11, 0.1)',
      danger: '#ef4444',
      dangerBg: 'rgba(239, 68, 68, 0.1)',
      info: '#06b6d4',
      infoBg: 'rgba(6, 182, 212, 0.1)',
      profitGreen: '#22c55e',
      lossRed: '#ef4444',
      bullish: '#22c55e',
      bearish: '#ef4444',
      neutral: '#94a3b8',
      chart1: '#3b82f6',
      chart2: '#22c55e',
      chart3: '#f59e0b',
      chart4: '#8b5cf6',
      chart5: '#ec4899',
      chart6: '#06b6d4',
      chart7: '#f97316',
      chart8: '#14b8a6',
      skeletonBase: '#1e293b',
      skeletonShimmer: '#334155',
      badgeBg: '#1e293b',
      scrollbarThumb: '#334155',
      scrollbarTrack: '#1e293b',
      radiusSm: '0.25rem',
      radiusMd: '0.5rem',
      radiusLg: '0.75rem',
    },
  },
  {
    id: 'terminal-green',
    name: 'Terminal Green',
    tokens: {
      bgDashboard: '#0a0a0a',
      bgPanel: '#111111',
      bgPanelHover: '#1a1a1a',
      bgHeader: '#0d0d0d',
      bgInput: '#1a1a1a',
      bgModal: '#151515',
      bgTooltip: '#1a1a1a',
      textPrimary: '#22c55e',
      textSecondary: '#86efac',
      textMuted: '#4ade80',
      textInverse: '#0a0a0a',
      borderDefault: '#1f2a1f',
      borderFocus: '#22c55e',
      borderError: '#ef4444',
      accentPrimary: '#22c55e',
      accentPrimaryHover: '#16a34a',
      success: '#22c55e',
      successBg: 'rgba(34, 197, 94, 0.15)',
      warning: '#facc15',
      warningBg: 'rgba(250, 204, 21, 0.1)',
      danger: '#ef4444',
      dangerBg: 'rgba(239, 68, 68, 0.1)',
      info: '#22d3ee',
      infoBg: 'rgba(34, 211, 238, 0.1)',
      profitGreen: '#4ade80',
      lossRed: '#f87171',
      bullish: '#4ade80',
      bearish: '#f87171',
      neutral: '#86efac',
      chart1: '#22c55e',
      chart2: '#4ade80',
      chart3: '#facc15',
      chart4: '#a78bfa',
      chart5: '#f472b6',
      chart6: '#22d3ee',
      chart7: '#fb923c',
      chart8: '#2dd4bf',
      skeletonBase: '#1a1a1a',
      skeletonShimmer: '#2a2a2a',
      badgeBg: '#1f2a1f',
      scrollbarThumb: '#2a3a2a',
      scrollbarTrack: '#1a1a1a',
      radiusSm: '0.125rem',
      radiusMd: '0.25rem',
      radiusLg: '0.375rem',
    },
  },
  {
    id: 'arctic-slate',
    name: 'Arctic Slate',
    tokens: {
      bgDashboard: '#0f172a',
      bgPanel: '#1e293b',
      bgPanelHover: '#293548',
      bgHeader: '#0f172a',
      bgInput: '#334155',
      bgModal: '#1e293b',
      bgTooltip: '#334155',
      textPrimary: '#e2e8f0',
      textSecondary: '#94a3b8',
      textMuted: '#64748b',
      textInverse: '#0f172a',
      borderDefault: '#334155',
      borderFocus: '#06b6d4',
      borderError: '#f43f5e',
      accentPrimary: '#06b6d4',
      accentPrimaryHover: '#0891b2',
      success: '#10b981',
      successBg: 'rgba(16, 185, 129, 0.1)',
      warning: '#eab308',
      warningBg: 'rgba(234, 179, 8, 0.1)',
      danger: '#f43f5e',
      dangerBg: 'rgba(244, 63, 94, 0.1)',
      info: '#38bdf8',
      infoBg: 'rgba(56, 189, 248, 0.1)',
      profitGreen: '#10b981',
      lossRed: '#f43f5e',
      bullish: '#10b981',
      bearish: '#f43f5e',
      neutral: '#94a3b8',
      chart1: '#06b6d4',
      chart2: '#10b981',
      chart3: '#eab308',
      chart4: '#a855f7',
      chart5: '#ec4899',
      chart6: '#38bdf8',
      chart7: '#f97316',
      chart8: '#14b8a6',
      skeletonBase: '#334155',
      skeletonShimmer: '#475569',
      badgeBg: '#334155',
      scrollbarThumb: '#475569',
      scrollbarTrack: '#1e293b',
      radiusSm: '0.375rem',
      radiusMd: '0.5rem',
      radiusLg: '0.75rem',
    },
  },
  {
    id: 'ember-dark',
    name: 'Ember Dark',
    tokens: {
      bgDashboard: '#18120a',
      bgPanel: '#1f1812',
      bgPanelHover: '#2a221a',
      bgHeader: '#140f0a',
      bgInput: '#2a221a',
      bgModal: '#1f1812',
      bgTooltip: '#2a221a',
      textPrimary: '#fef3c7',
      textSecondary: '#fcd34d',
      textMuted: '#d97706',
      textInverse: '#18120a',
      borderDefault: '#3a2a1a',
      borderFocus: '#f59e0b',
      borderError: '#dc2626',
      accentPrimary: '#f59e0b',
      accentPrimaryHover: '#d97706',
      success: '#84cc16',
      successBg: 'rgba(132, 204, 22, 0.1)',
      warning: '#fbbf24',
      warningBg: 'rgba(251, 191, 36, 0.1)',
      danger: '#dc2626',
      dangerBg: 'rgba(220, 38, 38, 0.1)',
      info: '#fb923c',
      infoBg: 'rgba(251, 146, 60, 0.1)',
      profitGreen: '#84cc16',
      lossRed: '#dc2626',
      bullish: '#84cc16',
      bearish: '#dc2626',
      neutral: '#fcd34d',
      chart1: '#f59e0b',
      chart2: '#84cc16',
      chart3: '#fbbf24',
      chart4: '#c084fc',
      chart5: '#f472b6',
      chart6: '#fb923c',
      chart7: '#34d399',
      chart8: '#a3e635',
      skeletonBase: '#2a221a',
      skeletonShimmer: '#3a2a1a',
      badgeBg: '#3a2a1a',
      scrollbarThumb: '#4a3a2a',
      scrollbarTrack: '#2a221a',
      radiusSm: '0.25rem',
      radiusMd: '0.5rem',
      radiusLg: '0.75rem',
    },
  },
  {
    id: 'high-contrast',
    name: 'High Contrast',
    tokens: {
      bgDashboard: '#000000',
      bgPanel: '#0a0a0a',
      bgPanelHover: '#171717',
      bgHeader: '#000000',
      bgInput: '#171717',
      bgModal: '#0a0a0a',
      bgTooltip: '#171717',
      textPrimary: '#ffffff',
      textSecondary: '#e5e5e5',
      textMuted: '#a3a3a3',
      textInverse: '#000000',
      borderDefault: '#404040',
      borderFocus: '#fbbf24',
      borderError: '#ff0000',
      accentPrimary: '#fbbf24',
      accentPrimaryHover: '#f59e0b',
      success: '#00ff00',
      successBg: 'rgba(0, 255, 0, 0.15)',
      warning: '#ffff00',
      warningBg: 'rgba(255, 255, 0, 0.15)',
      danger: '#ff0000',
      dangerBg: 'rgba(255, 0, 0, 0.15)',
      info: '#00ffff',
      infoBg: 'rgba(0, 255, 255, 0.15)',
      profitGreen: '#00ff00',
      lossRed: '#ff0000',
      bullish: '#00ff00',
      bearish: '#ff0000',
      neutral: '#ffffff',
      chart1: '#fbbf24',
      chart2: '#00ff00',
      chart3: '#00ffff',
      chart4: '#ff00ff',
      chart5: '#ff6600',
      chart6: '#00ff99',
      chart7: '#ff0066',
      chart8: '#6600ff',
      skeletonBase: '#171717',
      skeletonShimmer: '#262626',
      badgeBg: '#262626',
      scrollbarThumb: '#525252',
      scrollbarTrack: '#171717',
      radiusSm: '0rem',
      radiusMd: '0.25rem',
      radiusLg: '0.5rem',
    },
  },
];
