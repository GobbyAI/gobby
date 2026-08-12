export interface PlanVersion {
  content: string;
  messageId?: string;
  timestamp: Date;
}

export interface Plan {
  id: string;
  title: string;
  versions: PlanVersion[];
  currentVersionIndex: number;
}
