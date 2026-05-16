// TradeOS Theme Store
// Manages theme presets, custom tokens, and CSS variable injection

import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { THEME_PRESETS, type ThemeTokens, type ThemePreset } from '@/types/theme';

interface ThemeStore {
  // State
  currentPresetId: string;
  customPresets: ThemePreset[];
  customTokenOverrides: Partial<ThemeTokens>;
  
  // Computed
  getCurrentTheme: () => ThemeTokens;
  getAllPresets: () => ThemePreset[];
  
  // Actions
  setPreset: (presetId: string) => void;
  setToken: (key: keyof ThemeTokens, value: string) => void;
  setTokens: (tokens: Partial<ThemeTokens>) => void;
  resetToPreset: () => void;
  saveAsCustomPreset: (name: string) => string;
  deleteCustomPreset: (presetId: string) => void;
  importTheme: (themeJson: string) => boolean;
  exportTheme: () => string;
  getShareUrl: () => string;
  applyFromUrl: (encoded: string) => boolean;
}

// Inject CSS variables into document
function injectCssVariables(tokens: ThemeTokens) {
  if (typeof document === 'undefined') return;
  
  const root = document.documentElement;
  
  // Map token names to CSS variable names
  const cssVarMap: Record<keyof ThemeTokens, string> = {
    bgDashboard: '--bg-dashboard',
    bgPanel: '--bg-panel',
    bgPanelHover: '--bg-panel-hover',
    bgHeader: '--bg-header',
    bgInput: '--bg-input',
    bgModal: '--bg-modal',
    bgTooltip: '--bg-tooltip',
    textPrimary: '--text-primary',
    textSecondary: '--text-secondary',
    textMuted: '--text-muted',
    textInverse: '--text-inverse',
    borderDefault: '--border-default',
    borderFocus: '--border-focus',
    borderError: '--border-error',
    accentPrimary: '--accent-primary',
    accentPrimaryHover: '--accent-primary-hover',
    success: '--success',
    successBg: '--success-bg',
    warning: '--warning',
    warningBg: '--warning-bg',
    danger: '--danger',
    dangerBg: '--danger-bg',
    info: '--info',
    infoBg: '--info-bg',
    profitGreen: '--profit-green',
    lossRed: '--loss-red',
    bullish: '--bullish',
    bearish: '--bearish',
    neutral: '--neutral',
    chart1: '--chart-1',
    chart2: '--chart-2',
    chart3: '--chart-3',
    chart4: '--chart-4',
    chart5: '--chart-5',
    chart6: '--chart-6',
    chart7: '--chart-7',
    chart8: '--chart-8',
    skeletonBase: '--skeleton-base',
    skeletonShimmer: '--skeleton-shimmer',
    badgeBg: '--badge-bg',
    scrollbarThumb: '--scrollbar-thumb',
    scrollbarTrack: '--scrollbar-track',
    radiusSm: '--radius-sm',
    radiusMd: '--radius-md',
    radiusLg: '--radius-lg',
  };
  
  Object.entries(tokens).forEach(([key, value]) => {
    const cssVar = cssVarMap[key as keyof ThemeTokens];
    if (cssVar) {
      root.style.setProperty(cssVar, value);
    }
  });
}

export const useThemeStore = create<ThemeStore>()(
  persist(
    (set, get) => ({
      // Initial state
      currentPresetId: 'midnight-navy',
      customPresets: [],
      customTokenOverrides: {},
      
      // Get merged theme tokens
      getCurrentTheme: () => {
        const { currentPresetId, customPresets, customTokenOverrides } = get();
        const allPresets = [...THEME_PRESETS, ...customPresets];
        const preset = allPresets.find((p) => p.id === currentPresetId) || THEME_PRESETS[0];
        const merged = { ...preset.tokens, ...customTokenOverrides };
        return merged;
      },
      
      // Get all available presets
      getAllPresets: () => {
        const { customPresets } = get();
        return [...THEME_PRESETS, ...customPresets];
      },
      
      // Set active preset
      setPreset: (presetId: string) => {
        set({ currentPresetId: presetId, customTokenOverrides: {} });
        const theme = get().getCurrentTheme();
        injectCssVariables(theme);
      },
      
      // Set single token override
      setToken: (key: keyof ThemeTokens, value: string) => {
        set((state) => ({
          customTokenOverrides: { ...state.customTokenOverrides, [key]: value },
        }));
        const theme = get().getCurrentTheme();
        injectCssVariables(theme);
      },
      
      // Set multiple token overrides
      setTokens: (tokens: Partial<ThemeTokens>) => {
        set((state) => ({
          customTokenOverrides: { ...state.customTokenOverrides, ...tokens },
        }));
        const theme = get().getCurrentTheme();
        injectCssVariables(theme);
      },
      
      // Reset overrides to current preset
      resetToPreset: () => {
        set({ customTokenOverrides: {} });
        const theme = get().getCurrentTheme();
        injectCssVariables(theme);
      },
      
      // Save current theme as custom preset
      saveAsCustomPreset: (name: string) => {
        const theme = get().getCurrentTheme();
        const id = `custom-${Date.now()}`;
        const newPreset: ThemePreset = { id, name, tokens: theme };
        
        set((state) => ({
          customPresets: [...state.customPresets, newPreset],
          currentPresetId: id,
          customTokenOverrides: {},
        }));
        
        return id;
      },
      
      // Delete custom preset
      deleteCustomPreset: (presetId: string) => {
        const { currentPresetId } = get();
        set((state) => ({
          customPresets: state.customPresets.filter((p) => p.id !== presetId),
          currentPresetId: currentPresetId === presetId ? 'midnight-navy' : currentPresetId,
        }));
        
        if (get().currentPresetId === 'midnight-navy') {
          const theme = get().getCurrentTheme();
          injectCssVariables(theme);
        }
      },
      
      // Import theme from JSON
      importTheme: (themeJson: string) => {
        try {
          const imported = JSON.parse(themeJson) as ThemePreset;
          if (!imported.name || !imported.tokens) return false;
          
          const id = `imported-${Date.now()}`;
          const newPreset: ThemePreset = { ...imported, id };
          
          set((state) => ({
            customPresets: [...state.customPresets, newPreset],
            currentPresetId: id,
            customTokenOverrides: {},
          }));
          
          const theme = get().getCurrentTheme();
          injectCssVariables(theme);
          return true;
        } catch {
          return false;
        }
      },
      
      // Export current theme as JSON
      exportTheme: () => {
        const theme = get().getCurrentTheme();
        const { currentPresetId, customPresets } = get();
        const allPresets = [...THEME_PRESETS, ...customPresets];
        const preset = allPresets.find((p) => p.id === currentPresetId);
        
        return JSON.stringify(
          {
            name: preset?.name || 'Custom Theme',
            tokens: theme,
          },
          null,
          2
        );
      },
      
      // Get shareable URL with encoded theme
      getShareUrl: () => {
        if (typeof window === 'undefined') return '';
        const themeJson = get().exportTheme();
        const encoded = btoa(themeJson);
        return `${window.location.origin}${window.location.pathname}?theme=${encoded}`;
      },
      
      // Apply theme from URL-encoded string
      applyFromUrl: (encoded: string) => {
        try {
          const themeJson = atob(encoded);
          return get().importTheme(themeJson);
        } catch {
          return false;
        }
      },
    }),
    {
      name: 'tradeos-theme',
      partialize: (state) => ({
        currentPresetId: state.currentPresetId,
        customPresets: state.customPresets,
        customTokenOverrides: state.customTokenOverrides,
      }),
    }
  )
);

// Initialize theme on client
export function initializeTheme() {
  if (typeof window === 'undefined') return;
  
  // Check for theme in URL
  const urlParams = new URLSearchParams(window.location.search);
  const themeParam = urlParams.get('theme');
  
  if (themeParam) {
    useThemeStore.getState().applyFromUrl(themeParam);
  } else {
    const theme = useThemeStore.getState().getCurrentTheme();
    injectCssVariables(theme);
  }
}

// Hook for subscribing to theme changes
export function useCurrentTheme(): ThemeTokens {
  return useThemeStore((state) => state.getCurrentTheme());
}

export default useThemeStore;
