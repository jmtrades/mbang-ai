// Operator AI — window.Operator. Hand-written renditions of web/src/components/ui.tsx (React 18).
import type * as React from "react";

export type Tone = "neutral" | "good" | "bad" | "warn" | "brand" | "demo" | "info";

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  /** secondary is the default; one primary per view; danger stays outlined until hover. */
  tone?: "primary" | "secondary" | "ghost" | "subtle" | "danger" | "good";
  /** sm 28px · md 36px · lg 44px · icon 36px square · icon-sm 28px square */
  size?: "sm" | "md" | "lg" | "icon" | "icon-sm";
  /** Shows a spinner, keeps the size, sets aria-busy and disables the button. */
  loading?: boolean;
}
export interface IconButtonProps extends Omit<ButtonProps, "aria-label"> {
  /** Required: the accessible name (also the tooltip). */
  label: string;
}
export interface BadgeProps { tone?: Tone; dot?: boolean; title?: string; /** 600 weight and 0.04em tracking, as the DEMO badge. */ caps?: boolean; className?: string; children: React.ReactNode; }
export interface PillProps { tone?: "neutral" | "good" | "muted"; className?: string; children: React.ReactNode; }
export interface KbdProps { className?: string; children: React.ReactNode; }
export interface AvatarProps { /** Initials are derived; "(DEMO)" suffixes are stripped. */ name: string; small?: boolean; className?: string; }
export interface BrandMarkProps { name: string; /** An https image URL saved in Setup; falls back to the monogram. */ logo?: string | null; small?: boolean; className?: string; }
export interface CardProps { title?: React.ReactNode; subtitle?: React.ReactNode; actions?: React.ReactNode; /** false removes body padding (tables, lists). */ pad?: boolean; /** Lifts to sh-md on hover. */ hover?: boolean; className?: string; bodyClassName?: string; children?: React.ReactNode; }
export interface StatProps { label: string; value: React.ReactNode; hint?: React.ReactNode; tone?: "good" | "bad" | "warn"; /** Shows the DEMO badge on the label row. */ demo?: boolean; icon?: React.ReactNode; }
export interface PageHeaderProps { title: React.ReactNode; subtitle?: React.ReactNode; eyebrow?: React.ReactNode; badge?: React.ReactNode; right?: React.ReactNode; }
export interface FieldProps { label: string; hint?: string; children: React.ReactNode; }
export type InputProps = React.InputHTMLAttributes<HTMLInputElement>;
export type TextAreaProps = React.TextareaHTMLAttributes<HTMLTextAreaElement>;
export interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> { options: (string | { value: string; label: string })[]; }
export interface TabsProps { tabs: { key: string; label: React.ReactNode }[]; value: string; onChange?: (key: string) => void; }
export interface SegmentedProps<T extends string = string> { label: string; value: T; onChange?: (v: T) => void; options: { value: T; label: React.ReactNode; icon?: React.ReactNode }[]; }
export interface TableProps<R = any> { cols: { key: string; label: string; render?: (row: R) => React.ReactNode; className?: string }[]; rows: R[]; empty?: string; onRow?: (row: R) => void; selected?: (row: R) => boolean; }
export interface EmptyProps { title: string; hint?: React.ReactNode; icon?: React.ReactNode; action?: React.ReactNode; className?: string; }
export interface ErrorBoxProps { error: Error | string | null; retry?: () => void; }
export interface SkeletonProps { /** Renders this many shimmer lines of varying width. */ rows?: number; className?: string; style?: React.CSSProperties; }
export interface ToastProps { tone?: "good" | "bad" | "info"; onDismiss?: () => void; children: React.ReactNode; }
export interface Cite { id: string; file: string; title?: string; lines?: string; ts?: string; snippet?: string; }
export interface CitationProps { cite: Cite; onOpen?: (c: Cite) => void; }
export interface CitationListProps { items: Cite[]; onOpen?: (c: Cite) => void; }
export interface Step { tool: string; label?: string; status: "ok" | "done" | "error" | "failed" | "skipped" | "running" | "pending"; ms?: number; input?: unknown; output?: unknown; }
export interface RunStepsProps { steps: Step[]; compact?: boolean; defaultOpen?: number | null; }
export interface ModalProps { open?: boolean; onClose?: () => void; title: React.ReactNode; wide?: boolean; children: React.ReactNode; }
export interface Command { id: string; label: string; icon?: string; hint?: string; shortcut?: string; run?: () => void; }
export interface CommandPaletteProps { sections: { name: "Recent" | "Actions" | "Pages" | "Results"; items: Command[] }[]; query?: string; active?: number; }
export interface NavItemProps { label: string; icon?: string; href?: string; active?: boolean; onSelect?: () => void; }
export interface DrawerProps { open?: boolean; onClose?: () => void; title: React.ReactNode; children: React.ReactNode; }
export interface ToasterProps { toasts: { id: number | string; msg: string; tone?: "good" | "bad" | "info" }[]; onDismiss?: (id: number | string) => void; }
export interface SidebarProps { brand: string; logo?: string | null; tenant?: string; page?: string; sections: { group: string; items: { key: string; label: string; icon?: string }[] }[]; onNavigate?: (key: string) => void; user?: string; role?: string; onLogout?: () => void; }
export interface TabBarProps { tabs: { key: string; label: string; short?: string; icon?: string }[]; page?: string; onNavigate?: (key: string) => void; onMore?: () => void; }
export interface IconProps { /** A lucide name carried by the bundle: check, x, search, inbox, file-text, circle-check, … */ name: string; size?: number; strokeWidth?: number; spin?: boolean; className?: string; }

export const Button: React.FC<ButtonProps>;
export const IconButton: React.FC<IconButtonProps>;
export const Badge: React.FC<BadgeProps>;
export const Pill: React.FC<PillProps>;
export const Kbd: React.FC<KbdProps>;
export const Avatar: React.FC<AvatarProps>;
export const BrandMark: React.FC<BrandMarkProps>;
export const Card: React.FC<CardProps>;
export const Stat: React.FC<StatProps>;
export const PageHeader: React.FC<PageHeaderProps>;
export const Field: React.FC<FieldProps>;
export const Input: React.FC<InputProps>;
export const TextArea: React.FC<TextAreaProps>;
export const Select: React.FC<SelectProps>;
export const Tabs: React.FC<TabsProps>;
export const Segmented: React.FC<SegmentedProps>;
export const Table: React.FC<TableProps>;
export const Empty: React.FC<EmptyProps>;
export const ErrorBox: React.FC<ErrorBoxProps>;
export const Skeleton: React.FC<SkeletonProps>;
export const Toast: React.FC<ToastProps>;
export const Citation: React.FC<CitationProps> & { List: React.FC<CitationListProps> };
export const RunSteps: React.FC<RunStepsProps>;
export const Modal: React.FC<ModalProps>;
export const CommandPalette: React.FC<CommandPaletteProps>;
export const NavItem: React.FC<NavItemProps> & { Group: React.FC<{ label: string; children: React.ReactNode }> };
export const Drawer: React.FC<DrawerProps>;
export const Toaster: React.FC<ToasterProps>;
export const Sidebar: React.FC<SidebarProps>;
export const TabBar: React.FC<TabBarProps>;
export const Icon: React.FC<IconProps>;
