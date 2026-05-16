'use client';

import { ReactNode } from 'react';
import { AlertCircle, Database, RefreshCw, WifiOff } from 'lucide-react';
import { Button } from '@/components/ui/button';

interface DataGuardProps<T> {
  data: T | null | undefined;
  isLoading?: boolean;
  error?: Error | null;
  isEmpty?: boolean | ((data: T) => boolean);
  
  // Custom content
  loadingContent?: ReactNode;
  errorContent?: ReactNode;
  emptyContent?: ReactNode;
  
  // Empty state customization
  emptyIcon?: ReactNode;
  emptyTitle?: string;
  emptyDescription?: string;
  emptyAction?: {
    label: string;
    onClick: () => void;
  };
  
  // Error retry
  onRetry?: () => void;
  
  // Children render
  children: (data: T) => ReactNode;
  
  // Styling
  className?: string;
  minHeight?: string;
}

// Skeleton components for common use cases
export function SkeletonCard({ className = '' }: { className?: string }) {
  return (
    <div className={`panel p-4 ${className}`}>
      <div className="skeleton h-4 w-1/3 rounded mb-3" />
      <div className="skeleton h-8 w-1/2 rounded mb-4" />
      <div className="skeleton h-3 w-2/3 rounded" />
    </div>
  );
}

export function SkeletonTable({ rows = 5, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <div className="panel p-4">
      {/* Header */}
      <div className="flex gap-4 mb-4 pb-3 border-b border-border">
        {Array.from({ length: cols }).map((_, i) => (
          <div key={i} className="skeleton h-4 flex-1 rounded" />
        ))}
      </div>
      {/* Rows */}
      {Array.from({ length: rows }).map((_, rowIndex) => (
        <div key={rowIndex} className="flex gap-4 py-3">
          {Array.from({ length: cols }).map((_, colIndex) => (
            <div key={colIndex} className="skeleton h-4 flex-1 rounded" />
          ))}
        </div>
      ))}
    </div>
  );
}

export function SkeletonChart({ className = '' }: { className?: string }) {
  const heights = [55, 72, 38, 85, 61, 90, 44, 78, 52, 95, 67, 41];
  
  return (
    <div className={`panel p-4 ${className}`}>
      <div className="skeleton h-4 w-1/4 rounded mb-4" />
      <div className="flex items-end gap-2 h-48">
        {heights.map((h, i) => (
          <div
            key={i}
            className="skeleton flex-1 rounded-t"
            style={{ height: `${h}%` }}
          />
        ))}
      </div>
      <div className="flex justify-between mt-4">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="skeleton h-3 w-8 rounded" />
        ))}
      </div>
    </div>
  );
}

export function SkeletonKPI() {
  return (
    <div className="panel p-4">
      <div className="skeleton h-3 w-20 rounded mb-2" />
      <div className="skeleton h-8 w-24 rounded mb-1" />
      <div className="skeleton h-3 w-16 rounded" />
    </div>
  );
}

// Loading state component
function LoadingState({ 
  content, 
  minHeight 
}: { 
  content?: ReactNode; 
  minHeight?: string;
}) {
  if (content) return <>{content}</>;
  
  return (
    <div 
      className="flex items-center justify-center"
      style={{ minHeight: minHeight || '200px' }}
    >
      <div className="flex flex-col items-center gap-3">
        <div className="relative">
          <div className="h-10 w-10 rounded-full border-2 border-border" />
          <div className="absolute inset-0 h-10 w-10 rounded-full border-2 border-t-accent-primary animate-spin" />
        </div>
        <span className="text-sm text-muted-foreground">Loading data...</span>
      </div>
    </div>
  );
}

// Error state component
function ErrorState({
  error,
  content,
  onRetry,
  minHeight,
}: {
  error: Error;
  content?: ReactNode;
  onRetry?: () => void;
  minHeight?: string;
}) {
  if (content) return <>{content}</>;

  const isNetworkError = error.message.includes('network') || 
                         error.message.includes('fetch') ||
                         error.message.includes('NETWORK_ERROR');

  return (
    <div 
      className="flex items-center justify-center"
      style={{ minHeight: minHeight || '200px' }}
    >
      <div className="flex flex-col items-center gap-4 text-center max-w-sm px-4">
        <div className="p-3 rounded-full bg-danger-subtle">
          {isNetworkError ? (
            <WifiOff className="h-6 w-6 text-danger" />
          ) : (
            <AlertCircle className="h-6 w-6 text-danger" />
          )}
        </div>
        <div>
          <h3 className="font-medium text-foreground mb-1">
            {isNetworkError ? 'Connection Error' : 'Failed to Load Data'}
          </h3>
          <p className="text-sm text-muted-foreground">
            {isNetworkError
              ? 'Please check your connection and try again.'
              : error.message || 'An unexpected error occurred.'}
          </p>
        </div>
        {onRetry && (
          <Button
            variant="outline"
            size="sm"
            onClick={onRetry}
            className="gap-2"
          >
            <RefreshCw className="h-4 w-4" />
            Retry
          </Button>
        )}
      </div>
    </div>
  );
}

// Empty state component
function EmptyState({
  content,
  icon,
  title,
  description,
  action,
  minHeight,
}: {
  content?: ReactNode;
  icon?: ReactNode;
  title?: string;
  description?: string;
  action?: { label: string; onClick: () => void };
  minHeight?: string;
}) {
  if (content) return <>{content}</>;

  return (
    <div 
      className="flex items-center justify-center"
      style={{ minHeight: minHeight || '200px' }}
    >
      <div className="flex flex-col items-center gap-4 text-center max-w-sm px-4">
        <div className="p-3 rounded-full bg-muted">
          {icon || <Database className="h-6 w-6 text-muted-foreground" />}
        </div>
        <div>
          <h3 className="font-medium text-foreground mb-1">
            {title || 'No Data Available'}
          </h3>
          <p className="text-sm text-muted-foreground">
            {description || 'There is no data to display at this time.'}
          </p>
        </div>
        {action && (
          <Button variant="outline" size="sm" onClick={action.onClick}>
            {action.label}
          </Button>
        )}
      </div>
    </div>
  );
}

// Main DataGuard component
export function DataGuard<T>({
  data,
  isLoading,
  error,
  isEmpty,
  loadingContent,
  errorContent,
  emptyContent,
  emptyIcon,
  emptyTitle,
  emptyDescription,
  emptyAction,
  onRetry,
  children,
  className,
  minHeight,
}: DataGuardProps<T>) {
  // Loading state
  if (isLoading) {
    return (
      <div className={className}>
        <LoadingState content={loadingContent} minHeight={minHeight} />
      </div>
    );
  }

  // Error state
  if (error) {
    return (
      <div className={className}>
        <ErrorState 
          error={error} 
          content={errorContent} 
          onRetry={onRetry}
          minHeight={minHeight}
        />
      </div>
    );
  }

  // Check if data is empty
  const dataIsEmpty = data === null || 
                      data === undefined || 
                      (typeof isEmpty === 'function' ? isEmpty(data) : isEmpty) ||
                      (Array.isArray(data) && data.length === 0);

  // Empty state
  if (dataIsEmpty) {
    return (
      <div className={className}>
        <EmptyState
          content={emptyContent}
          icon={emptyIcon}
          title={emptyTitle}
          description={emptyDescription}
          action={emptyAction}
          minHeight={minHeight}
        />
      </div>
    );
  }

  // Render children with data
  return <div className={className}>{children(data as T)}</div>;
}

export default DataGuard;
