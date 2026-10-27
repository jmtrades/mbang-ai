// Academy: short cited lessons from the programme's own material, questions that come back before you forget,
// role-play practice and certificates. Team and Admin tabs appear for coaches and the owner.
import { useEffect, useState } from "react";
import { BookOpen } from "lucide-react";
import { Badge, Card, DemoBadge, Empty, ErrorBox, Loading, PageHeader, Pill, Tabs, useApi, useApp } from "../components/ui";
import { RolePlayPanel } from "./academy/RolePlay";
import { CertificatesTab, DemoMaterialBadge, LessonReader, PathsTab, PracticeTab, TodayTab } from "./academy/Learn";
import { AdminTab, TeamTab } from "./academy/Staff";

export default function Academy({ query }: { query?: URLSearchParams }) {
  const { me } = useApp();
  const role = me.user.role;
  const staff = role === "owner" || role === "coach";
  const tabs = [
    { key: "today", label: "Today" },
    { key: "paths", label: "Paths" },
    { key: "practice", label: "Practice" },
    { key: "roleplay", label: "Role-play" },
    { key: "certs", label: "Certificates" },
    ...(staff ? [{ key: "team", label: "Team" }] : []),
    ...(role === "owner" ? [{ key: "admin", label: "Admin" }] : []),
  ];
  const initial = query?.get("tab") || "today";
  const [tab, setTab] = useState(tabs.some((t) => t.key === initial) ? initial : "today");
  const [lesson, setLesson] = useState<number | null>(Number(query?.get("lesson")) || null);
  const { data, error, loading, reload } = useApi<any>("/academy");
  useEffect(() => {
    const t = query?.get("tab");
    if (t && tabs.some((x) => x.key === t)) setTab(t);
    setLesson(Number(query?.get("lesson")) || null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query?.toString()]);
  const switchTab = (t: string) => {
    setLesson(null);
    setTab(t);
  };
  const empty = data && !data.built.published;
  return (
    <div>
      <PageHeader
        title="Academy"
        subtitle="Short lessons from your programme's own material, each line cited. Questions come back just before you'd forget them, and certificates show training done."
        badge={me.is_demo ? <DemoBadge /> : undefined}
        right={data && (
          <span className="flex flex-wrap items-center gap-2">
            {data.material.demo_material && <DemoMaterialBadge />}
            <Pill>{data.ai_label}</Pill>
          </span>
        )}
      />
      <Tabs tabs={tabs} value={tab} onChange={switchTab} />
      {loading && !data && <Loading rows={5} />}
      <ErrorBox error={error} retry={reload} />
      {lesson ? (
        <LessonReader id={lesson} onBack={() => { setLesson(null); reload(); }} onRolePlay={() => switchTab("roleplay")} />
      ) : (
        data && (
          <>
            {empty && !["team", "admin", "roleplay"].includes(tab) && (
              <Card className="mb-4" title={data.material.ok ? "No lessons published yet" : "Load your course material first"}>
                <Empty
                  icon={<BookOpen className="size-6" />}
                  title={data.material.ok ? (role === "owner" ? "Build the Academy, then publish the drafts" : "Your lessons are on their way") : "The Academy only teaches from the owner's real material"}
                  hint={
                    data.material.ok
                      ? role === "owner"
                        ? "Open the Admin tab and press Build now. Every lesson arrives as a draft for you to check against its source."
                        : "The owner reviews every lesson before it reaches you. Role-play practice is open in the meantime."
                      : role === "owner"
                        ? data.material.reason
                        : "The owner hasn't loaded the course material yet, so there is nothing to learn from here. Nothing fictional is ever taught as real."
                  }
                />
              </Card>
            )}
            {tab === "today" && !empty && <TodayTab data={data} reload={reload} openLesson={setLesson} setTab={switchTab} />}
            {tab === "paths" && !empty && <PathsTab openLesson={setLesson} />}
            {tab === "practice" && !empty && <PracticeTab />}
            {tab === "roleplay" && (
              <div className="space-y-3">
                {data.suggested_roleplay && <p className="text-sm text-muted">Suggested focus today: <Badge>{data.suggested_roleplay.label}</Badge></p>}
                <RolePlayPanel />
              </div>
            )}
            {tab === "certs" && <CertificatesTab setTab={switchTab} />}
            {tab === "team" && staff && <TeamTab />}
            {tab === "admin" && role === "owner" && <AdminTab />}
          </>
        )
      )}
    </div>
  );
}
