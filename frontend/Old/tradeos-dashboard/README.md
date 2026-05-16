# TradeOS Intelligence Console

A dark-themed trading intelligence dashboard for a systematic NSE Indian equity swing trading system.

## Features

- **5 Dashboard Tabs**: Performance, Positions, AI Intel, Brain Engine, Data Management
- **Real-time Charts**: Win rate trends, signal breakdowns, P&L analytics, conviction accuracy
- **Interactive Tables**: Sortable, filterable, inline editing
- **Modals**: Price updates, position exits, config changes with audit trails
- **DataGuard Pattern**: Every chart/table has intelligent empty/low-data states
- **Dark Theme**: Navy-based design system with semantic colors

## Tech Stack

- React 18 + Vite
- Tailwind CSS
- Recharts
- Lucide React icons
- TanStack Table (ready for integration)

## Quick Start

```bash
# Install dependencies
npm install

# Start dev server
npm run dev

# Build for production
npm run build
```

## Project Structure

```
src/
  components/       # Reusable UI components (DataGuard, Modal, Button, etc.)
  components/ui/    # shadcn-style primitives
  tabs/             # 5 main dashboard tabs
  data/             # Mock data for development
  lib/              # Utilities (formatting, cn helper)
  App.jsx           # Root layout with tab navigation
  main.jsx          # Entry point
```

## Connecting to Backend

The dashboard is wired for a FastAPI backend. Update API calls in each tab to point to your endpoints:

- `GET /api/performance/metrics`
- `GET /api/positions/open`
- `POST /api/positions/open`
- `PUT /api/positions/{id}/price`
- `POST /api/positions/{id}/close`
- `GET /api/ai-context`
- `GET /api/brain/proposals`
- `POST /api/brain/proposals/{id}/approve`
- `GET /api/config`
- `PUT /api/config/{key}`

For Supabase realtime subscriptions, wire up the channels in `App.jsx`.

## Design Tokens

All colors use CSS custom properties defined in `tailwind.config.js` and `index.css`:

- `--bg-base`: #0a0f1e
- `--bg-surface`: #111827
- `--trade-green`: #10b981
- `--trade-red`: #ef4444
- `--trade-blue`: #3b82f6
- etc.

## License

Proprietary — TradeOS System
