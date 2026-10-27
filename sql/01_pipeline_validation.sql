/* Data-Pipeline-01 Frequent admissions with 2 and more admissions within 365 days 
   - Definition: >= 2 admissions within 365 days: Current admission + 1 prior admission within past 365 day
   - Planned admissions excluded (admission_type = 'ELECTIVE', 'SURGICAL SAME DAY ADMISSION')
*/

create or replace view frequent_admissions 
as
select adm.subject_id, adm.hadm_id, adm.admittime, adm.deathtime, count(hist.hadm_id) as admission_frequency
from admissions adm, admissions hist
where adm.subject_id = hist.subject_id
and adm.admission_type not in ('ELECTIVE', 'SURGICAL SAME DAY ADMISSION')
and hist.admittime < adm.admittime -- No data leakage
and hist.admittime >= date_sub(adm.admittime, interval 365 day)
group by adm.subject_id, adm.hadm_id, adm.admittime, adm.deathtime
having count(hist.hadm_id) >= 1;

/* Data-Pipeline-02 Ward stays of frequent admissions (no overlap with ICU stays)

*/
  
create or replace view ward_stays
as
select fa.subject_id, fa.hadm_id, fa.deathtime, t.transfer_id, t.careunit, t.intime ward_intime, t.outtime ward_outtime
from frequent_admissions fa, transfers t 
where fa.hadm_id = t.hadm_id
and t.eventtype in ('admit', 'transfer')
and t.careunit is not null
and t.outtime is not null  -- For training only
and t.careunit not like '%Obstetrics%'
and t.careunit not in ('PACU', 'Observation', 'Emergency Department','Emergency Department Observation', 'Discharge Lounge', 'Labor & Delivery', 'Special Care Nursery(SCN)','Psychiatry', 'UNKNOWN', 'unknown')
and not exists (
			select 1
            from icustays i
            where i.hadm_id = t.hadm_id
            and t.intime < i.outtime
            and t.outtime > i.intime);
            
/* Data-Pipeline-03 Daily snapshot */

create table if not exists seq_days (k int primary key);
insert ignore into seq_days (k)
with recursive s as (select 0 as k union all select k + 1 from s where k < 365)
select k from s;

create or replace view ward_snapshots 
as
select subject_id,hadm_id,transfer_id,careunit,ward_intime,ward_outtime,deathtime, s.k snapshot_day, date_add(ward_intime, interval s.k day) predict_time
from ward_stays w, seq_days s
where  s.k >= 1
and date_add(w.ward_intime, interval s.k day) < w.ward_outtime
and (w.deathtime is null
or date_add(w.ward_intime, interval s.k day) < w.deathtime);


/* Data-Pipeline-04 ICU transfer classification */

create or replace view icu_transfer_class
as
select s.subject_id, s.hadm_id, s.transfer_id, s.careunit, s.ward_intime, s.ward_outtime, s.snapshot_day, s.predict_time, min(i.intime) as icu_intime, 
	timestampdiff(minute, s.predict_time, min(i.intime)) / 60.0 as hours_to_icu,
	case when min(i.intime) <= s.predict_time + interval 24 hour then 1 else 0 end icu_transfer_24h,
	case when min(i.intime) <= s.predict_time + interval 48 hour then 1 else 0 end icu_transfer_48h
from ward_snapshots s left join icustays i
on s.hadm_id = i.hadm_id
and s.predit_time < i.intime
group by s.subject_id, s.hadm_id, s.transfer_id, s.careunit, s.ward_intime, s.ward_outtime, s.snapshot_day, s.predict_time;
