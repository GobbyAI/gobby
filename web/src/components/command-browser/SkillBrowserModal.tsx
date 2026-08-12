import { useState, useEffect, useMemo, useCallback } from "react";
import type { GobbySkill } from "../../hooks/useSkills";
import { Input } from "../ui/Input";
import { Button } from "../ui/Button";
import { Badge } from "../ui/Badge";
import { ScrollArea } from "../ui/ScrollArea";
import { cn } from "../../lib/utils";
import { Heading } from "../shared/Heading";

interface SkillBrowserModalProps {
  onSendMessage: (content: string, injectContext: string) => void;
  onClose: () => void;
}

export function SkillBrowserModal({
  onSendMessage,
  onClose,
}: SkillBrowserModalProps) {
  const [skills, setSkills] = useState<GobbySkill[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [selectedSkillId, setSelectedSkillId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fetchSkills = async () => {
      try {
        const resp = await fetch("/api/skills?enabled=true&limit=200");
        if (resp.ok) {
          const data = await resp.json();
          if (!cancelled) setSkills(data.skills || []);
        }
      } catch (e) {
        console.error("Failed to fetch skills:", e);
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };
    fetchSkills();
    return () => {
      cancelled = true;
    };
  }, []);

  const filtered = useMemo(() => {
    if (!search) return skills;
    const lower = search.toLowerCase();
    return skills.filter(
      (s) =>
        s.name.toLowerCase().includes(lower) ||
        s.description?.toLowerCase().includes(lower),
    );
  }, [skills, search]);

  const selectedSkill = useMemo(
    () => skills.find((s) => s.id === selectedSkillId) ?? null,
    [skills, selectedSkillId],
  );

  const handleRun = useCallback(() => {
    if (selectedSkill) {
      onSendMessage(
        `Run skill: ${selectedSkill.name}`,
        selectedSkill.content || "",
      );
      onClose();
    }
  }, [selectedSkill, onSendMessage, onClose]);

  const sourceBadge = (skill: GobbySkill) => {
    if (skill.source === "template")
      return <Badge variant="default">template</Badge>;
    if (skill.source === "project")
      return <Badge variant="info">project</Badge>;
    if (skill.hub_name) return <Badge variant="success">hub</Badge>;
    return <Badge variant="default">installed</Badge>;
  };

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex shrink-0 items-center justify-between border-b border-border bg-muted/30 px-4 py-3">
        <div className="flex items-center gap-2">
          <SkillsIcon />
          <Heading level={2} className="text-lg font-semibold text-foreground">
            Skills
          </Heading>
          {!isLoading && (
            <span className="text-xs text-muted-foreground">
              ({filtered.length})
            </span>
          )}
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          dense
          onClick={onClose}
          className="min-h-0 w-auto rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          aria-label="Close"
        >
          <XIcon />
        </Button>
      </div>

      {/* Mobile: stacked layout. Desktop: side-by-side */}
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        {/* Left panel: skill list */}
        <div
          className={cn(
            "flex min-h-0 flex-col border-border",
            selectedSkill
              ? "hidden md:flex md:w-[40%] md:border-r"
              : "w-full md:w-[40%] md:border-r",
          )}
        >
          <div className="shrink-0 border-b border-border p-3">
            <Input
              type="text"
              placeholder="Search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="bg-muted/50"
            />
          </div>
          <ScrollArea className="flex-1">
            {isLoading ? (
              <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
                <SpinnerIcon />
                Loading skills...
              </div>
            ) : filtered.length === 0 ? (
              <p className="p-4 text-sm text-muted-foreground">
                {search
                  ? "No skills match your search."
                  : "No enabled skills found."}
              </p>
            ) : (
              filtered.map((skill) => (
                <Button
                  key={skill.id}
                  type="button"
                  variant="ghost"
                  size="sm"
                  dense
                  className={cn(
                    "min-h-0 w-full items-stretch justify-start rounded-none border-x-0 border-t-0 border-b border-border/30 px-3 py-2.5 text-left text-sm font-normal whitespace-normal transition-colors",
                    selectedSkillId === skill.id
                      ? "bg-accent/15 text-foreground"
                      : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                  )}
                  onClick={() => setSelectedSkillId(skill.id)}
                >
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-foreground">
                      {skill.name}
                    </span>
                    {sourceBadge(skill)}
                  </div>
                  {skill.description && (
                    <div className="mt-0.5 truncate text-xs opacity-60">
                      {skill.description}
                    </div>
                  )}
                </Button>
              ))
            )}
          </ScrollArea>
        </div>

        {/* Right panel: skill preview */}
        <div
          className={cn(
            "flex min-h-0 flex-col",
            selectedSkill ? "flex-1" : "hidden flex-1 md:flex",
          )}
        >
          {!selectedSkill ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 p-4 text-sm text-muted-foreground">
              <SkillsIcon size={32} />
              <span>Select a skill to preview it</span>
            </div>
          ) : (
            <>
              {/* Mobile back button */}
              <Button
                type="button"
                variant="ghost"
                size="sm"
                dense
                className="flex min-h-0 shrink-0 items-center justify-start gap-1 rounded-none border-x-0 border-t-0 border-b border-border px-3 py-2 text-sm text-accent hover:bg-muted/50 md:hidden"
                onClick={() => setSelectedSkillId(null)}
              >
                <ChevronLeftIcon />
                Back to list
              </Button>

              <div className="shrink-0 border-b border-border bg-muted/20 px-4 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-semibold text-foreground">
                    {selectedSkill.name}
                  </span>
                  {sourceBadge(selectedSkill)}
                  {selectedSkill.always_apply && (
                    <Badge variant="warning">always-apply</Badge>
                  )}
                </div>
                {selectedSkill.description && (
                  <p className="mt-1 text-sm text-muted-foreground">
                    {selectedSkill.description}
                  </p>
                )}
                {selectedSkill.version && (
                  <span className="text-xs text-muted-foreground">
                    v{selectedSkill.version}
                  </span>
                )}
              </div>

              <ScrollArea className="flex-1 px-4 py-3">
                <pre className="rounded-md border border-border/50 bg-muted/50 p-3 font-mono text-xs whitespace-pre-wrap text-foreground">
                  {selectedSkill.content || "(no content)"}
                </pre>
              </ScrollArea>

              <div className="shrink-0 border-t border-border bg-muted/20 px-4 py-3">
                <Button variant="accent" onClick={handleRun}>
                  Run Skill
                </Button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function XIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}

function ChevronLeftIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <polyline points="15 18 9 12 15 6" />
    </svg>
  );
}

function SkillsIcon({ size = 18 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="text-accent"
    >
      <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
    </svg>
  );
}

function SpinnerIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      className="animate-spin"
    >
      <circle
        cx="12"
        cy="12"
        r="10"
        strokeDasharray="32"
        strokeDashoffset="32"
      />
    </svg>
  );
}
