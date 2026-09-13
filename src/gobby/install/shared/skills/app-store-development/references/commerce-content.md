# Commerce and user content

Load only for payments, monetization, accounts tied to purchases, shared content,
or specialized app categories. This is a decision checklist, not a frozen law table.

## Payments

Before recommending a purchase path, identify the thing sold (digital feature,
subscription, physical good/service), where it is consumed, app category, platform,
distribution channel and storefront. Inspect contracts and actual entitlements.
An IP address or UI language alone is not reliable storefront evidence.

Recheck [Guidelines 3.1.1–3.1.3](https://developer.apple.com/app-store/review/guidelines/)
and their linked regional/entitlement documentation. Supported alternatives can
include StoreKit or an applicable external-purchase path; neither “always IAP” nor
“one external link everywhere” is an adequate determination. US rules do not imply
EU permission. Record the exception, eligibility, disclosures, entitlement,
contract and allowed UI for each affected storefront before implementing routing.
If evidence is missing, leave that decision unresolved and keep independent work
moving. Do not make up a permanent list of regions, fees or exceptions.

For subscriptions verify restoration, access state, renewal/cancellation disclosure
and handling of purchase failures. Separate account deletion from cancellation.
Keep extension monetization subject to the specific extension rules in 4.4.

## Shared content and special categories

Determine whether user content is merely private local data or is published/shared
through an app-operated service. A private screenshot notebook is not automatically
a social network. For public user-generated content assess current Guideline 1.2:
moderation, reports, blocking, contact routes and abuse response. Consider age
ratings and rights to third-party material before adding a sharing service.

Kids, health, finance, gambling, VPN, browser engines and other regulated/entitled
features need their specific current Apple sources and applicable jurisdictional
review. Identify the topic and fetch that evidence when triggered; this initial
skill set does not certify every specialized category. Do not invent blanket
prohibitions when a scoped requirement or supported alternative exists.
