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
      alerts: {
        Row: {
          comparison_mode: string
          baseline_price: number | null
          baseline_at: string | null
          observed_at: string | null
          change_pct: number
          created_at: string
          data_source: string
          id: string
          is_test: boolean
          price: number | null
          rule: string
          symbol: string
          threshold_pct: number
          triggered_at: string
          user_id: string
          window_minutes: number | null
        }
        Insert: {
          comparison_mode?: string
          baseline_price?: number | null
          baseline_at?: string | null
          observed_at?: string | null
          change_pct: number
          created_at?: string
          data_source: string
          id?: string
          is_test?: boolean
          price?: number | null
          rule: string
          symbol: string
          threshold_pct: number
          triggered_at?: string
          user_id: string
          window_minutes: number | null
        }
        Update: {
          comparison_mode?: string
          baseline_price?: number | null
          baseline_at?: string | null
          observed_at?: string | null
          change_pct?: number
          created_at?: string
          data_source?: string
          id?: string
          is_test?: boolean
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
          user_id: string
          symbol: string
          baseline_price: number
          baseline_at: string
          data_source: string
          threshold_pct: number
          last_observed_at: string
          last_up_alert_at: string | null
          last_down_alert_at: string | null
        }
        Insert: {
          user_id: string
          symbol: string
          baseline_price: number
          baseline_at: string
          data_source: string
          threshold_pct: number
          last_observed_at: string
          last_up_alert_at?: string | null
          last_down_alert_at?: string | null
        }
        Update: {
          user_id?: string
          symbol?: string
          baseline_price?: number
          baseline_at?: string
          data_source?: string
          threshold_pct?: number
          last_observed_at?: string
          last_up_alert_at?: string | null
          last_down_alert_at?: string | null
        }
        Relationships: [{
          foreignKeyName: "monitor_baselines_user_id_symbol_fkey"
          columns: ["user_id", "symbol"]
          isOneToOne: true
          referencedRelation: "watchlist_items"
          referencedColumns: ["user_id", "symbol"]
        }]
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
          p_user_id: string
          p_symbol: string
          p_price: number
          p_observed_at: string
          p_source: string
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
