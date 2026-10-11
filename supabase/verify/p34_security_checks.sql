-- P34 / #275: run each check in the new project's Supabase SQL Editor.
-- Project: xshzjhocpcenuumdrhdh. These queries are read-only.
-- Verification is pending; migration text alone does not prove live privileges.

-- a. RLS: expect zero rows.
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p')
  AND NOT c.relrowsecurity;

-- b. Effective write access: expect zero protected_results_table = true rows.
-- Report rows when the role has INSERT/UPDATE/DELETE privilege and either RLS
-- is off or a policy applies to that role and command.
-- Other returned rows need PR justification or a fix.
-- Applicable policies include ALL commands, PUBLIC, and inherited role access.
SELECT access_role.rolname AS role_name, c.relname AS table_name,
       write_command.command,
       c.relname ~ '(forward|paper|signal|outcome|setup)' AS protected_results_table
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN pg_roles access_role
CROSS JOIN (VALUES ('INSERT', 'a'::"char"),
                   ('UPDATE', 'w'::"char"),
                   ('DELETE', 'd'::"char")) AS write_command(command, policy_command)
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p')
  AND access_role.rolname IN ('anon', 'authenticated')
  AND has_table_privilege(access_role.oid, c.oid, write_command.command)
  AND (
    NOT c.relrowsecurity
    OR EXISTS (
      SELECT 1
      FROM pg_policy policy
      WHERE policy.polrelid = c.oid
        AND (policy.polcmd = '*' OR policy.polcmd = write_command.policy_command)
        AND (
          0::oid = ANY (policy.polroles)
          OR EXISTS (
            SELECT 1
            FROM pg_roles policy_role
            WHERE policy_role.oid = ANY (policy.polroles)
              AND pg_has_role(access_role.oid, policy_role.oid, 'USAGE')
          )
        )
    )
  )
ORDER BY table_name, role_name, write_command.command;

-- c. List every public SECURITY DEFINER function and every commit function.
-- Commit RPCs also use SECURITY INVOKER, so include them explicitly.
-- Every commit function must have anon_exec = false, auth_exec = false,
-- and service_role_exec = true. Return these flags along with the row count.
SELECT p.proname, p.oid::regprocedure AS function_signature,
       p.prosecdef AS security_definer,
       has_function_privilege('anon', p.oid, 'EXECUTE') AS anon_exec,
       has_function_privilege('authenticated', p.oid, 'EXECUTE') AS auth_exec,
       has_function_privilege('service_role', p.oid, 'EXECUTE') AS service_role_exec
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND (p.prosecdef OR p.proname LIKE 'commit%')
ORDER BY p.proname, function_signature;

-- d. Extensions: expect two rows, named pg_cron and pg_net.
SELECT extname
FROM pg_extension
WHERE extname IN ('pg_cron', 'pg_net')
ORDER BY extname;
