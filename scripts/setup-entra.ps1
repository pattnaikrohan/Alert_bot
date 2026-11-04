<#
.SYNOPSIS
  Registers Alert Bot in Microsoft Entra ID: an API app + a portal (SPA) app, sign-in limited to assigned
  users, and (optionally) a Conditional Access policy that requires Microsoft Authenticator.

.DESCRIPTION
  Creates tenant objects, so run it deliberately, signed in (`az login`) as someone allowed to register apps
  (Application Developer / Cloud Application Administrator). The Conditional Access step needs Entra ID P1 and
  Conditional Access Administrator; it is created in REPORT-ONLY mode so nobody is locked out; switch it to On
  in the Entra admin centre after checking its sign-in log.

  The API enforces MFA on its own as well (ALERTBOT_REQUIRE_MFA=true): it rejects any token whose `amr` claim
  shows no second factor, so a gap in Conditional Access never lets a password-only sign-in through.

.EXAMPLE
  ./scripts/setup-entra.ps1 -Users chinmoy.jagjit@contoso.com -CreateConditionalAccess
#>
param(
  [string]$PortalUrl = "http://localhost:5173",
  [string[]]$Users = @(),
  [switch]$CreateConditionalAccess
)
$ErrorActionPreference = "Stop"

function Graph([string]$Method, [string]$Path, $Body) {
  $azArgs = @("rest", "--method", $Method, "--url", "https://graph.microsoft.com/v1.0$Path")
  if ($null -ne $Body) {
    $file = New-TemporaryFile
    ($Body | ConvertTo-Json -Depth 10) | Out-File -FilePath $file -Encoding utf8
    $azArgs += @("--headers", "Content-Type=application/json", "--body", "@$file")
  }
  $out = az @args
  if ($LASTEXITCODE -ne 0) { throw "Graph $Method $Path failed" }
  if ($out) { return ($out | ConvertFrom-Json) }
}

$tenant = az account show --query tenantId -o tsv
Write-Host "Tenant: $tenant"

# ── API app: what the portal's tokens are issued for ─────────────────────────
$api = az ad app create --display-name "Alert Bot API" --sign-in-audience AzureADMyOrg | ConvertFrom-Json
$scopeId = [guid]::NewGuid().ToString()
az ad app update --id $api.appId --identifier-uris "api://$($api.appId)" | Out-Null
Graph PATCH "/applications/$($api.id)" @{
  api = @{
    requestedAccessTokenVersion = 2          # the API validates v2.0 issuers
    oauth2PermissionScopes = @(@{
      id = $scopeId; value = "access_as_user"; type = "User"; isEnabled = $true
      adminConsentDisplayName = "Use Alert Bot"; adminConsentDescription = "Sign in to the Alert Bot portal and read your projects' alerts."
      userConsentDisplayName = "Use Alert Bot"; userConsentDescription = "Read the Alert Bot projects you belong to."
    })
  }
} | Out-Null
az ad sp create --id $api.appId | Out-Null

# ── Portal app (single-page app, no secret) ──────────────────────────────────
$spa = az ad app create --display-name "Alert Bot Portal" --sign-in-audience AzureADMyOrg | ConvertFrom-Json
Graph PATCH "/applications/$($spa.id)" @{
  spa = @{ redirectUris = @($PortalUrl) }
  requiredResourceAccess = @(@{ resourceAppId = $api.appId; resourceAccess = @(@{ id = $scopeId; type = "Scope" }) })
} | Out-Null
# Pre-authorise the portal so users get no consent prompt.
Graph PATCH "/applications/$($api.id)" @{
  api = @{ preAuthorizedApplications = @(@{ appId = $spa.appId; delegatedPermissionIds = @($scopeId) }) }
} | Out-Null
$spaSp = az ad sp create --id $spa.appId | ConvertFrom-Json

# Only people assigned to the app can sign in at all (project access is then decided by Alert Bot itself).
az ad sp update --id $spa.appId --set appRoleAssignmentRequired=true | Out-Null
foreach ($u in $Users) {
  $uid = az ad user show --id $u --query id -o tsv
  Graph POST "/servicePrincipals/$($spaSp.id)/appRoleAssignedTo" @{
    principalId = $uid; resourceId = $spaSp.id; appRoleId = "00000000-0000-0000-0000-000000000000"
  } | Out-Null
  Write-Host "Assigned $u to Alert Bot Portal"
}

# ── Conditional Access: require Microsoft Authenticator (MFA) ────────────────
if ($CreateConditionalAccess) {
  $policy = Graph POST "/identity/conditionalAccess/policies" @{
    displayName = "Alert Bot: require MFA (Microsoft Authenticator)"
    state = "enabledForReportingButNotEnforced"   # report-only first; turn On after checking the sign-in log
    conditions = @{
      users = @{ includeUsers = @("All") }
      applications = @{ includeApplications = @($api.appId, $spa.appId) }
      clientAppTypes = @("all")
    }
    # Built-in authentication strength "Multifactor authentication" (Authenticator push / number match, etc.).
    grantControls = @{ operator = "OR"; authenticationStrength = @{ id = "00000000-0000-0000-0000-000000000002" } }
  }
  Write-Host "Conditional Access policy created in report-only mode: $($policy.id)"
}

Write-Host ""
Write-Host "── .env.live ──"
Write-Host "ALERTBOT_AUTH_MODE=entra"
Write-Host "ALERTBOT_ENTRA_TENANT_ID=$tenant"
Write-Host "ALERTBOT_ENTRA_API_AUDIENCE=$($api.appId)"   # v2 tokens carry the API client id as aud
Write-Host "ALERTBOT_REQUIRE_MFA=true"
Write-Host ""
Write-Host "── portal/.env.local ──"
Write-Host "VITE_ENTRA_CLIENT_ID=$($spa.appId)"
Write-Host "VITE_ENTRA_TENANT_ID=$tenant"
Write-Host "VITE_ENTRA_API_SCOPE=api://$($api.appId)/access_as_user"
