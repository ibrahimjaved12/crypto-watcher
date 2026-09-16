export type Json =
  | string
  | number
  | boolean
  | null
  | { [key: string]: Json | undefined }
  | Json[]

export type Database = {
  // Allows to automatically instantiate createClient with right options
  // instead of createClient<Database, { PostgrestVersion: 'XX' }>(URL, KEY)
  __InternalSupabase: {
    PostgrestVersion: "14.5"
  }
  public: {
    Tables: {
      ta_signals: {
        Row: {
          id: string; user_id: string; symbol: string; timeframe: number;
          candle_at: string; detected_at: string; source: string; version: string;
          price: number; indicators: Json; patterns: string[]; outcome_status: string;
          outcome_at: string | null; outcome_price: number | null; return_pct: number | null;
        }
        Insert: {
          id?: string; user_id: string; symbol: string; timeframe: number;
          candle_at: string; detected_at?: string; source: string; version: string;
          price: number; indicators: Json; patterns: string[]; outcome_status?: string;
          outcome_at?: string | null; outcome_price?: number | null; return_pct?: number | null;
        }
        Update: { outcome_at?: string; outcome_price?: number; return_pct?: number; outcome_status?: string }
        Relationships: []
      }
      alerts: {
        Row: {
          baseline_at: string | null
          baseline_price: number | null
          change_pct: number
          comparison_mode: string
          created_at: string
          data_source: string
          id: string
          is_test: boolean
          observed_at: string | null
          price: number | null
          rule: string
          symbol: string
          threshold_pct: number
          triggered_at: string
          user_id: string
          window_minutes: number | null
        }
        Insert: {
          baseline_at?: string | null
          baseline_price?: number | null
          change_pct: number
          comparison_mode?: string
          created_at?: string
          data_source: string
          id?: string
          is_test?: boolean
          observed_at?: string | null
          price?: number | null
          rule: string
          symbol: string
          threshold_pct: number
          triggered_at?: string
          user_id: string
          window_minutes?: number | null
        }
        Update: {
          baseline_at?: string | null
          baseline_price?: number | null
          change_pct?: number
          comparison_mode?: string
          created_at?: string
          data_source?: string
          id?: string
          is_test?: boolean
          observed_at?: string | null
          price?: number | null
          rule?: string
          symbol?: string
          threshold_pct?: number
          triggered_at?: string
          user_id?: string
          window_minutes?: number | null
        }
        Relationships: []
      }
      monitor_baselines: {
        Row: {
          baseline_at: string
          baseline_price: number
          data_source: string
          last_down_alert_at: string | null
          last_observed_at: string
          last_up_alert_at: string | null
          symbol: string
          threshold_pct: number
          user_id: string
        }
        Insert: {
          baseline_at: string
          baseline_price: number
          data_source: string
          last_down_alert_at?: string | null
          last_observed_at: string
          last_up_alert_at?: string | null
          symbol: string
          threshold_pct: number
          user_id: string
        }
        Update: {
          baseline_at?: string
          baseline_price?: number
          data_source?: string
          last_down_alert_at?: string | null
          last_observed_at?: string
          last_up_alert_at?: string | null
          symbol?: string
          threshold_pct?: number
          user_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "monitor_baselines_user_id_symbol_fkey"
            columns: ["user_id", "symbol"]
            isOneToOne: true
            referencedRelation: "watchlist_items"
            referencedColumns: ["user_id", "symbol"]
          },
        ]
      }
      monitor_runs: {
        Row: {
          alerts_created: number
          data_source: string | null
          error_message: string | null
          id: string
          ran_at: string
          status: string
          symbols_checked: number
          user_id: string | null
        }
        Insert: {
          alerts_created?: number
          data_source?: string | null
          error_message?: string | null
          id?: string
          ran_at?: string
          status: string
          symbols_checked?: number
          user_id?: string | null
        }
        Update: {
          alerts_created?: number
          data_source?: string | null
          error_message?: string | null
          id?: string
          ran_at?: string
          status?: string
          symbols_checked?: number
          user_id?: string | null
        }
        Relationships: []
      }
      monitor_settings: {
        Row: {
          cooldown_minutes: number
          created_at: string
          monitoring_enabled: boolean
          threshold_pct: number
          updated_at: string
          user_id: string
          window_minutes: number
        }
        Insert: {
          cooldown_minutes?: number
          created_at?: string
          monitoring_enabled?: boolean
          threshold_pct?: number
          updated_at?: string
          user_id: string
          window_minutes?: number
        }
        Update: {
          cooldown_minutes?: number
          created_at?: string
          monitoring_enabled?: boolean
          threshold_pct?: number
          updated_at?: string
          user_id?: string
          window_minutes?: number
        }
        Relationships: []
      }
      notes: {
        Row: {
          body: string
          created_at: string
          id: string
          symbol: string | null
          title: string
          updated_at: string
          user_id: string
        }
        Insert: {
          body?: string
          created_at?: string
          id?: string
          symbol?: string | null
          title: string
          updated_at?: string
          user_id: string
        }
        Update: {
          body?: string
          created_at?: string
          id?: string
          symbol?: string | null
          title?: string
          updated_at?: string
          user_id?: string
        }
        Relationships: []
      }
      watchlist_items: {
        Row: {
          created_at: string
          id: string
          symbol: string
          user_id: string
        }
        Insert: {
          created_at?: string
          id?: string
          symbol: string
          user_id: string
        }
        Update: {
          created_at?: string
          id?: string
          symbol?: string
          user_id?: string
        }
        Relationships: []
      }
    }
    Views: {
      [_ in never]: never
    }
    Functions: {
      process_cumulative_observation: {
        Args: {
          p_observed_at: string
          p_price: number
          p_source: string
          p_symbol: string
          p_user_id: string
        }
        Returns: Json
      }
    }
    Enums: {
      [_ in never]: never
    }
    CompositeTypes: {
      [_ in never]: never
    }
  }
}

type DatabaseWithoutInternals = Omit<Database, "__InternalSupabase">

type DefaultSchema = DatabaseWithoutInternals[Extract<keyof Database, "public">]

export type Tables<
  DefaultSchemaTableNameOrOptions extends
    | keyof (DefaultSchema["Tables"] & DefaultSchema["Views"])
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends (DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof (DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"] &
        DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Views"])
    : never) = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? (DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"] &
      DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Views"])[TableName] extends {
      Row: infer R
    }
    ? R
    : never
  : DefaultSchemaTableNameOrOptions extends keyof (DefaultSchema["Tables"] &
        DefaultSchema["Views"])
    ? (DefaultSchema["Tables"] &
        DefaultSchema["Views"])[DefaultSchemaTableNameOrOptions] extends {
        Row: infer R
      }
      ? R
      : never
    : never

export type TablesInsert<
  DefaultSchemaTableNameOrOptions extends
    | keyof DefaultSchema["Tables"]
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends (DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"]
    : never) = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"][TableName] extends {
      Insert: infer I
    }
    ? I
    : never
  : DefaultSchemaTableNameOrOptions extends keyof DefaultSchema["Tables"]
    ? DefaultSchema["Tables"][DefaultSchemaTableNameOrOptions] extends {
        Insert: infer I
      }
      ? I
      : never
    : never

export type TablesUpdate<
  DefaultSchemaTableNameOrOptions extends
    | keyof DefaultSchema["Tables"]
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends (DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"]
    : never) = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"][TableName] extends {
      Update: infer U
    }
    ? U
    : never
  : DefaultSchemaTableNameOrOptions extends keyof DefaultSchema["Tables"]
    ? DefaultSchema["Tables"][DefaultSchemaTableNameOrOptions] extends {
        Update: infer U
      }
      ? U
      : never
    : never

export type Enums<
  DefaultSchemaEnumNameOrOptions extends
    | keyof DefaultSchema["Enums"]
    | { schema: keyof DatabaseWithoutInternals },
  EnumName extends (DefaultSchemaEnumNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaEnumNameOrOptions["schema"]]["Enums"]
    : never) = never,
> = DefaultSchemaEnumNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaEnumNameOrOptions["schema"]]["Enums"][EnumName]
  : DefaultSchemaEnumNameOrOptions extends keyof DefaultSchema["Enums"]
    ? DefaultSchema["Enums"][DefaultSchemaEnumNameOrOptions]
    : never

export type CompositeTypes<
  PublicCompositeTypeNameOrOptions extends
    | keyof DefaultSchema["CompositeTypes"]
    | { schema: keyof DatabaseWithoutInternals },
  CompositeTypeName extends (PublicCompositeTypeNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[PublicCompositeTypeNameOrOptions["schema"]]["CompositeTypes"]
    : never) = never,
> = PublicCompositeTypeNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[PublicCompositeTypeNameOrOptions["schema"]]["CompositeTypes"][CompositeTypeName]
  : PublicCompositeTypeNameOrOptions extends keyof DefaultSchema["CompositeTypes"]
    ? DefaultSchema["CompositeTypes"][PublicCompositeTypeNameOrOptions]
    : never

export const Constants = {
  public: {
    Enums: {},
  },
} as const
