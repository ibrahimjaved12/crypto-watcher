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
          baseline_at: string | null
          baseline_price: number | null
          change_pct: number
          comparison_mode: string
          created_at: string
          data_source: string
          endpoint: string
          id: string
          instrument_id: string
          is_test: boolean
          observed_at: string | null
          price: number | null
          price_type: string
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
          endpoint?: string
          id?: string
          instrument_id: string
          is_test?: boolean
          observed_at?: string | null
          price?: number | null
          price_type?: string
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
          endpoint?: string
          id?: string
          instrument_id?: string
          is_test?: boolean
          observed_at?: string | null
          price?: number | null
          price_type?: string
          rule?: string
          symbol?: string
          threshold_pct?: number
          triggered_at?: string
          user_id?: string
          window_minutes?: number | null
        }
        Relationships: [
          {
            foreignKeyName: "alerts_instrument_identity_fkey"
            columns: ["instrument_id", "symbol"]
            isOneToOne: false
            referencedRelation: "market_instruments"
            referencedColumns: ["id", "native_symbol"]
          },
        ]
      }
      analysis_conclusions: {
        Row: {
          classification: string
          completed_candle_at: string | null
          configuration_version: string
          contract_type: string
          detected_at: string
          direction: string
          endpoint: string
          evaluated_at: string
          factor_breakdown: Json
          id: string
          idempotency_key: string
          input_hash: string | null
          input_reference: string | null
          instrument_id: string
          market_type: string
          model_version: string | null
          persisted_at: string
          price_type: string
          provider: string
          reasons: Json
          reference_price: number | null
          schema_version: number
          score: number | null
          score_kind: string
          source_event_at: string | null
          source_freshness_ms: number | null
          source_instrument_id: string
          source_native_symbol: string
          source_retrieved_at: string | null
          status: string
          strategy_version: string
          supersedes_id: string | null
          symbol: string
          ta_version: string
          timeframe_minutes: number
          user_id: string
        }
        Insert: {
          classification: string
          completed_candle_at?: string | null
          configuration_version: string
          contract_type?: string
          detected_at: string
          direction: string
          endpoint: string
          evaluated_at: string
          factor_breakdown: Json
          id?: string
          idempotency_key: string
          input_hash?: string | null
          input_reference?: string | null
          instrument_id: string
          market_type?: string
          model_version?: string | null
          persisted_at?: string
          price_type: string
          provider: string
          reasons: Json
          reference_price?: number | null
          schema_version: number
          score?: number | null
          score_kind?: string
          source_event_at?: string | null
          source_freshness_ms?: number | null
          source_instrument_id: string
          source_native_symbol: string
          source_retrieved_at?: string | null
          status: string
          strategy_version: string
          supersedes_id?: string | null
          symbol: string
          ta_version: string
          timeframe_minutes: number
          user_id: string
        }
        Update: {
          classification?: string
          completed_candle_at?: string | null
          configuration_version?: string
          contract_type?: string
          detected_at?: string
          direction?: string
          endpoint?: string
          evaluated_at?: string
          factor_breakdown?: Json
          id?: string
          idempotency_key?: string
          input_hash?: string | null
          input_reference?: string | null
          instrument_id?: string
          market_type?: string
          model_version?: string | null
          persisted_at?: string
          price_type?: string
          provider?: string
          reasons?: Json
          reference_price?: number | null
          schema_version?: number
          score?: number | null
          score_kind?: string
          source_event_at?: string | null
          source_freshness_ms?: number | null
          source_instrument_id?: string
          source_native_symbol?: string
          source_retrieved_at?: string | null
          status?: string
          strategy_version?: string
          supersedes_id?: string | null
          symbol?: string
          ta_version?: string
          timeframe_minutes?: number
          user_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "analysis_conclusions_instrument_identity_fkey"
            columns: ["instrument_id", "symbol"]
            isOneToOne: false
            referencedRelation: "market_instruments"
            referencedColumns: ["id", "native_symbol"]
          },
          {
            foreignKeyName: "analysis_conclusions_supersedes_fkey"
            columns: ["user_id", "supersedes_id"]
            isOneToOne: false
            referencedRelation: "analysis_conclusions"
            referencedColumns: ["user_id", "id"]
          },
        ]
      }
      market_data_checkpoints: {
        Row: {
          data_source: string
          endpoint: string
          instrument_id: string
          observed_at: string
          price: number
          price_type: string
          symbol: string
          updated_at: string
          user_id: string
        }
        Insert: {
          data_source: string
          endpoint?: string
          instrument_id: string
          observed_at: string
          price: number
          price_type?: string
          symbol: string
          updated_at?: string
          user_id: string
        }
        Update: {
          data_source?: string
          endpoint?: string
          instrument_id?: string
          observed_at?: string
          price?: number
          price_type?: string
          symbol?: string
          updated_at?: string
          user_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "market_data_checkpoints_instrument_identity_fkey"
            columns: ["instrument_id", "symbol"]
            isOneToOne: false
            referencedRelation: "market_instruments"
            referencedColumns: ["id", "native_symbol"]
          },
          {
            foreignKeyName: "market_data_checkpoints_user_id_symbol_fkey"
            columns: ["user_id", "symbol"]
            isOneToOne: true
            referencedRelation: "watchlist_items"
            referencedColumns: ["user_id", "symbol"]
          },
        ]
      }
      market_instruments: {
        Row: {
          base_asset: string
          contract_multiplier: number
          contract_type: string
          exchange: string
          id: string
          is_linear: boolean
          margin_asset: string
          market_type: string
          native_symbol: string
          quote_asset: string
          settlement_asset: string
        }
        Insert: {
          base_asset: string
          contract_multiplier: number
          contract_type: string
          exchange: string
          id: string
          is_linear: boolean
          margin_asset: string
          market_type: string
          native_symbol: string
          quote_asset: string
          settlement_asset: string
        }
        Update: {
          base_asset?: string
          contract_multiplier?: number
          contract_type?: string
          exchange?: string
          id?: string
          is_linear?: boolean
          margin_asset?: string
          market_type?: string
          native_symbol?: string
          quote_asset?: string
          settlement_asset?: string
        }
        Relationships: []
      }
      monitor_baselines: {
        Row: {
          baseline_at: string
          baseline_price: number
          data_source: string
          endpoint: string
          instrument_id: string
          last_down_alert_at: string | null
          last_observed_at: string
          last_up_alert_at: string | null
          price_type: string
          symbol: string
          threshold_pct: number
          user_id: string
        }
        Insert: {
          baseline_at: string
          baseline_price: number
          data_source: string
          endpoint?: string
          instrument_id: string
          last_down_alert_at?: string | null
          last_observed_at: string
          last_up_alert_at?: string | null
          price_type?: string
          symbol: string
          threshold_pct: number
          user_id: string
        }
        Update: {
          baseline_at?: string
          baseline_price?: number
          data_source?: string
          endpoint?: string
          instrument_id?: string
          last_down_alert_at?: string | null
          last_observed_at?: string
          last_up_alert_at?: string | null
          price_type?: string
          symbol?: string
          threshold_pct?: number
          user_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "monitor_baselines_instrument_identity_fkey"
            columns: ["instrument_id", "symbol"]
            isOneToOne: false
            referencedRelation: "market_instruments"
            referencedColumns: ["id", "native_symbol"]
          },
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
          duration_ms: number | null
          error_message: string | null
          id: string
          metrics: Json
          ran_at: string
          status: string
          symbols_checked: number
          user_id: string | null
        }
        Insert: {
          alerts_created?: number
          data_source?: string | null
          duration_ms?: number | null
          error_message?: string | null
          id?: string
          metrics?: Json
          ran_at?: string
          status: string
          symbols_checked?: number
          user_id?: string | null
        }
        Update: {
          alerts_created?: number
          data_source?: string | null
          duration_ms?: number | null
          error_message?: string | null
          id?: string
          metrics?: Json
          ran_at?: string
          status?: string
          symbols_checked?: number
          user_id?: string | null
        }
        Relationships: []
      }
      monitor_settings: {
        Row: {
          completed_candle_ta_enabled: boolean
          cooldown_minutes: number
          created_at: string
          developing_setup_evaluation_enabled: boolean
          market_data_collection_enabled: boolean
          monitoring_enabled: boolean
          movement_alerts_enabled: boolean
          paper_trading_enabled: boolean
          threshold_pct: number
          updated_at: string
          user_id: string
          window_minutes: number
        }
        Insert: {
          completed_candle_ta_enabled?: boolean
          cooldown_minutes?: number
          created_at?: string
          developing_setup_evaluation_enabled?: boolean
          market_data_collection_enabled?: boolean
          monitoring_enabled?: boolean
          movement_alerts_enabled?: boolean
          paper_trading_enabled?: boolean
          threshold_pct?: number
          updated_at?: string
          user_id: string
          window_minutes?: number
        }
        Update: {
          completed_candle_ta_enabled?: boolean
          cooldown_minutes?: number
          created_at?: string
          developing_setup_evaluation_enabled?: boolean
          market_data_collection_enabled?: boolean
          monitoring_enabled?: boolean
          movement_alerts_enabled?: boolean
          paper_trading_enabled?: boolean
          threshold_pct?: number
          updated_at?: string
          user_id?: string
          window_minutes?: number
        }
        Relationships: []
      }
      notification_channel_preferences: {
        Row: {
          channel: string
          created_at: string
          delivery_enabled: boolean
          updated_at: string
          user_id: string
        }
        Insert: {
          channel: string
          created_at?: string
          delivery_enabled?: boolean
          updated_at?: string
          user_id: string
        }
        Update: {
          channel?: string
          created_at?: string
          delivery_enabled?: boolean
          updated_at?: string
          user_id?: string
        }
        Relationships: []
      }
      notes: {
        Row: {
          body: string
          created_at: string
          id: string
          instrument_id: string | null
          symbol: string | null
          title: string
          updated_at: string
          user_id: string
        }
        Insert: {
          body?: string
          created_at?: string
          id?: string
          instrument_id?: string | null
          symbol?: string | null
          title: string
          updated_at?: string
          user_id: string
        }
        Update: {
          body?: string
          created_at?: string
          id?: string
          instrument_id?: string | null
          symbol?: string | null
          title?: string
          updated_at?: string
          user_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "notes_instrument_identity_fkey"
            columns: ["instrument_id", "symbol"]
            isOneToOne: false
            referencedRelation: "market_instruments"
            referencedColumns: ["id", "native_symbol"]
          },
        ]
      }
      ta_signals: {
        Row: {
          atr_pct: number | null
          candle_at: string
          classification: string
          detected_at: string
          endpoint: string
          evaluated_at: string
          factor_breakdown: Json
          id: string
          indicators: Json
          instrument_id: string
          outcome_at: string | null
          outcome_price: number | null
          outcome_status: string
          patterns: string[]
          price: number
          price_type: string
          reasons: string[]
          return_pct: number | null
          score: number | null
          source: string
          source_event_at: string
          source_instrument_id: string
          source_native_symbol: string
          strategy_version: string
          symbol: string
          timeframe: number
          user_id: string
          version: string
        }
        Insert: {
          atr_pct?: number | null
          candle_at: string
          classification: string
          detected_at: string
          endpoint?: string
          evaluated_at: string
          factor_breakdown: Json
          id?: string
          indicators: Json
          instrument_id: string
          outcome_at?: string | null
          outcome_price?: number | null
          outcome_status?: string
          patterns: string[]
          price: number
          price_type?: string
          reasons: string[]
          return_pct?: number | null
          score?: number | null
          source: string
          source_event_at: string
          source_instrument_id: string
          source_native_symbol: string
          strategy_version: string
          symbol: string
          timeframe: number
          user_id: string
          version: string
        }
        Update: {
          atr_pct?: number | null
          candle_at?: string
          classification?: string
          detected_at?: string
          endpoint?: string
          evaluated_at?: string
          factor_breakdown?: Json
          id?: string
          indicators?: Json
          instrument_id?: string
          outcome_at?: string | null
          outcome_price?: number | null
          outcome_status?: string
          patterns?: string[]
          price?: number
          price_type?: string
          reasons?: string[]
          return_pct?: number | null
          score?: number | null
          source?: string
          source_event_at?: string
          source_instrument_id?: string
          source_native_symbol?: string
          strategy_version?: string
          symbol?: string
          timeframe?: number
          user_id?: string
          version?: string
        }
        Relationships: [
          {
            foreignKeyName: "ta_signals_instrument_identity_fkey"
            columns: ["instrument_id", "symbol"]
            isOneToOne: false
            referencedRelation: "market_instruments"
            referencedColumns: ["id", "native_symbol"]
          },
        ]
      }
      watchlist_items: {
        Row: {
          created_at: string
          id: string
          instrument_id: string
          symbol: string
          user_id: string
        }
        Insert: {
          created_at?: string
          id?: string
          instrument_id: string
          symbol: string
          user_id: string
        }
        Update: {
          created_at?: string
          id?: string
          instrument_id?: string
          symbol?: string
          user_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "watchlist_instrument_identity_fkey"
            columns: ["instrument_id", "symbol"]
            isOneToOne: false
            referencedRelation: "market_instruments"
            referencedColumns: ["id", "native_symbol"]
          },
        ]
      }
    }
    Views: {
      [_ in never]: never
    }
    Functions: {
      apply_ta_outcomes: {
        Args: {
          p_outcomes: Json
          p_user_id: string
        }
        Returns: number
      }
      get_ta_due_work: {
        Args: {
          p_include_generation: boolean
          p_include_outcomes: boolean
          p_now: string
          p_symbol: string
          p_user_id: string
          p_version: string
        }
        Returns: {
          candle_at: string
          detected_at: string
          id: string
          price: number
          source: string
          timeframe: number
          work_kind: string
        }[]
      }
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
      record_market_data_checkpoint: {
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
