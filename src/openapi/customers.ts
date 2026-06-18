/**
 * customers.ts
 * ────────────────────────────────────────────────────────────────────
 * **Customer** bundle. Schema matches the real APC v4.0.0 docs exactly
 * (verified from the Create Customer sample request/response, 2026-06-15).
 *
 *   POST /openapi/v4.0.0/customers          — Create Customer
 *   PUT  /openapi/v4.0.0/customers/{id}     — Update Customer
 *
 * Tekion wraps every customer in a `customerDetails` object and uses
 * deeply-nested communication / address / occupation structures. The full
 * shape is typed below; `buildIndividualCustomer()` is provided so callers
 * don't have to assemble the whole tree for the common case.
 */
import type { ApcClient, ApcResponse } from '../apc.js';

const VERSION = 'v4.0.0';
const BASE = `/${VERSION}/customers`;

// ─── Enums / unions ────────────────────────────────────────────────────

export type CustomerStatus = 'ACTIVE' | 'INACTIVE';
export type CustomerType = 'INDIVIDUAL' | 'BUSINESS';
export type AddressType = 'SHIPPING_ADDRESS' | 'BILLING_ADDRESS' | 'MAILING_ADDRESS';
export type PhoneType = 'MOBILE' | 'HOME' | 'WORK';
export type TimeUnit = 'YEAR' | 'MONTH' | 'DAY';

/** Per-channel marketing/transaction opt-in mapping. */
export interface PreferenceMapping {
  MARKETING?: 'YES' | 'NO';
  TRANSACTION?: 'YES' | 'NO';
}

export interface UsagePreference {
  preferred?: boolean;
  preferenceMapping?: PreferenceMapping;
}

export interface PhoneNumber {
  countryCode: number;
  localNumber: string;
}

// ─── Communications ────────────────────────────────────────────────────

export interface EmailCommunication {
  email: string;
  usagePreference?: UsagePreference;
}

export interface PhoneCommunication {
  phone: PhoneNumber;
  phoneType?: PhoneType;
  usagePreference?: UsagePreference;
}

export interface FaxCommunication {
  phone: PhoneNumber;
  usagePreference?: UsagePreference;
}

export interface SmsCommunication {
  phone: PhoneNumber;
  phoneType?: PhoneType;
  usagePreference?: UsagePreference;
}

export interface PostalEmailCommunication {
  customerId: string;
  usagePreference?: UsagePreference;
}

// ─── Address / name / occupation ───────────────────────────────────────

export interface GeoDetails {
  CITY?: string;
  PROVINCE?: string;
  COUNTY?: string;
}

export interface Address {
  addressType: AddressType;
  current: boolean;
  country: string;
  postalCode: string;
  addressLine1: string;
  addressLine2?: string;
  geoDetails?: GeoDetails;
}

export interface CustomerName {
  prefix?: string;
  firstName: string;
  middleName?: string;
  lastName: string;
  suffix?: string;
}

export interface TaxInformation {
  taxID: string;
}

export interface License {
  documentId: string;
  issueDate?: number;   // epoch ms
  expiryDate?: number;  // epoch ms
}

export interface Occupation {
  current?: boolean;
  employedBy?: {
    name?: { value: string };
    address?: {
      addressLine1?: string;
      addressLine2?: string;
      postalCode?: string;
      country?: string;
      geoDetails?: GeoDetails;
    };
  };
  period?: { value: number; timeUnit: TimeUnit };
}

// ─── Customer envelope ─────────────────────────────────────────────────

export interface CustomerDetails {
  customerType: CustomerType;
  name?: CustomerName;
  dob?: string; // "YYYY-MM-DD"
  emailCommunications?: EmailCommunication[];
  phoneCommunications?: PhoneCommunication[];
  faxCommunications?: FaxCommunication[];
  smsCommunications?: SmsCommunication[];
  postalEmailCommunications?: PostalEmailCommunication[];
  addresses?: Address[];
  taxInformation?: TaxInformation;
  license?: License;
  occupation?: Occupation;
  /** APC may add optional fields over time — keep this open. */
  [extra: string]: unknown;
}

export interface CustomerRequest {
  status: CustomerStatus;
  customerDetails: CustomerDetails;
}

/** The standard APC response envelope: { data, meta }. */
export interface ApcEnvelope<T> {
  data: T;
  meta: { status: string };
}

export interface CustomerData {
  vehicleIds?: string[];
  vehicles?: Array<{
    vin: string;
    last8DigitVIN?: string;
    vehicleId: string;
    year?: string;
    make?: string;
    model?: string;
    vehicleResource?: { id: string; reference: string };
  }>;
  customerId?: string;
  creationTime?: number;
  lastUpdateTime?: number;
  [extra: string]: unknown;
}

// ─── Endpoints ─────────────────────────────────────────────────────────

/** Create Customer — POST /openapi/v4.0.0/customers */
export function createCustomer(
  apc: ApcClient,
  customer: CustomerRequest,
): Promise<ApcResponse<ApcEnvelope<CustomerData>>> {
  return apc.post(BASE, customer);
}

/** Update Customer — PUT /openapi/v4.0.0/customers/{customerId} */
export function updateCustomer(
  apc: ApcClient,
  customerId: string,
  customer: Partial<CustomerRequest>,
): Promise<ApcResponse<ApcEnvelope<CustomerData>>> {
  return apc.put(`${BASE}/${encodeURIComponent(customerId)}`, customer);
}

// ─── Builder for the common case ───────────────────────────────────────

export interface SimpleIndividual {
  firstName: string;
  lastName: string;
  middleName?: string;
  prefix?: string;
  suffix?: string;
  email?: string;
  mobile?: string;          // local number
  countryCode?: number;     // defaults to 1 (US)
  dob?: string;             // "YYYY-MM-DD"
  address?: {
    line1: string;
    line2?: string;
    city?: string;
    state?: string;
    county?: string;
    postalCode: string;
    country?: string;       // defaults to "US"
  };
  taxId?: string;
  marketingOptIn?: boolean;
}

/**
 * Assemble a full Tekion CustomerRequest from a flat object — handles all the
 * nested `customerDetails` / communications / address wrapping for you.
 */
export function buildIndividualCustomer(input: SimpleIndividual): CustomerRequest {
  const cc = input.countryCode ?? 1;
  const pref: UsagePreference = {
    preferred: true,
    preferenceMapping: {
      MARKETING: input.marketingOptIn ? 'YES' : 'NO',
      TRANSACTION: 'YES',
    },
  };

  const details: CustomerDetails = {
    customerType: 'INDIVIDUAL',
    name: {
      prefix: input.prefix,
      firstName: input.firstName,
      middleName: input.middleName,
      lastName: input.lastName,
      suffix: input.suffix,
    },
    dob: input.dob,
  };

  if (input.email) {
    details.emailCommunications = [{ email: input.email, usagePreference: pref }];
  }
  if (input.mobile) {
    details.phoneCommunications = [{
      phone: { countryCode: cc, localNumber: input.mobile },
      phoneType: 'MOBILE',
      usagePreference: pref,
    }];
  }
  if (input.address) {
    details.addresses = [{
      addressType: 'BILLING_ADDRESS',
      current: true,
      country: input.address.country ?? 'US',
      postalCode: input.address.postalCode,
      addressLine1: input.address.line1,
      addressLine2: input.address.line2,
      geoDetails: {
        CITY: input.address.city,
        PROVINCE: input.address.state,
        COUNTY: input.address.county,
      },
    }];
  }
  if (input.taxId) {
    details.taxInformation = { taxID: input.taxId };
  }

  return { status: 'ACTIVE', customerDetails: details };
}
